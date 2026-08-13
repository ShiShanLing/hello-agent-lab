"""账号认证与 Agent 会话隔离测试。"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from hello_agent.api import SessionManager, create_api
from hello_agent.auth import SqliteAuthService
from hello_agent.admin_auth import SqliteAdminAuthService, _totp
from hello_agent.database import (
    SqliteConversationStore,
    SqliteTravelStore,
    SqliteTaskStore,
    dispose_database_connections,
)
from hello_agent.travel import TravelPlan


class FakeAgent:
    last_confirmation_requested = False
    last_action_cancelled = False
    last_sources: list[dict[str, object]] = []
    last_confidence = "none"
    last_grounding = "none"
    last_capture_grounding_failure = False

    def __init__(self) -> None:
        self.messages: list[object] = []
        self.model_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self.trace_steps: list[dict[str, object]] = []
        self.model = "deepseek-v4-flash"

    def estimated_cost_total(self) -> float:
        return 0.0

    def models_label(self) -> str:
        return self.model

    def reset_trace(self) -> None:
        self.trace_steps = []

    def grounding_payload(self) -> dict[str, object]:
        return {
            "sources": self.last_sources,
            "confidence": self.last_confidence,
            "grounding": self.last_grounding,
            "capture_grounding_failure": self.last_capture_grounding_failure,
        }

    def ask(self, message: str, approval_callback=None) -> str:
        answer = f"收到：{message}"
        self.messages = [
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer},
        ]
        return answer


class AuthAPITest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "agent.db"
        self.upload_directory = Path(self.temporary_directory.name) / "uploads"
        self.demo_directory = Path(self.temporary_directory.name) / "demos"
        self.demo_directory.mkdir()
        (self.demo_directory / "演示制度.md").write_text(
            "# 演示制度\n\n测试补贴为每天 88 元。",
            encoding="utf-8",
        )
        self.environment = patch.dict(
            "os.environ",
            {
                "KNOWLEDGE_UPLOAD_DIR": str(self.upload_directory),
                "DEMO_KNOWLEDGE_DIR": str(self.demo_directory),
            },
        )
        self.environment.start()
        auth = SqliteAuthService(self.database_path)
        manager = SessionManager(
            session_factory=lambda _session_id, _user_id: FakeAgent(),
            history_loader=lambda _session_id: [],
        )
        self.app = create_api(manager, auth_service=auth)
        self.client = TestClient(self.app)
        self.admin_auth = SqliteAdminAuthService(self.database_path)
        self.admin_auth.bootstrap(
            "system-admin@example.com",
            "temporary-admin-password-123",
            "独立管理员",
            can_manage_knowledge=True,
        )
        password_step = self.client.post(
            "/admin-auth/login",
            json={
                "email": "system-admin@example.com",
                "password": "temporary-admin-password-123",
            },
        ).json()
        setup = self.client.post(
            "/admin-auth/mfa/setup",
            json={"challenge_token": password_step["challenge_token"]},
        ).json()
        activated = self.client.post(
            "/admin-auth/mfa/activate",
            json={
                "challenge_token": password_step["challenge_token"],
                "code": _totp(setup["secret"], int(time.time() // 30)),
                "new_password": "independent-admin-password-456",
            },
        )
        self.assertEqual(activated.status_code, 200)
        self.recovery_codes = activated.json()["recovery_codes"]

    def tearDown(self) -> None:
        dispose_database_connections()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_registers_and_restores_login_from_http_only_cookie(self) -> None:
        response = self.client.post(
            "/auth/register",
            json={
                "email": "XiaoMing@example.com",
                "password": "password123",
                "display_name": "小明",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["email"], "xiaoming@example.com")
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        me = self.client.get("/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["display_name"], "小明")

    def test_lists_tasks_retries_failures_and_marks_notifications_read(self) -> None:
        registered = self.client.post(
            "/auth/register",
            json={
                "email": "task-user@example.com",
                "password": "password123",
                "display_name": "任务用户",
            },
        ).json()
        store = SqliteTaskStore(self.database_path, registered["id"])
        task = store.create("knowledge_index", "测试后台任务", {"document_id": "d"})
        store.mark_failed(task["id"], "测试失败")

        center = self.client.get("/tasks")
        self.assertEqual(center.status_code, 200)
        self.assertEqual(center.json()["tasks"][0]["status"], "failed")
        self.assertEqual(center.json()["unread_count"], 1)

        retried = self.client.post(f"/tasks/{task['id']}/retry")
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["status"], "queued")

        notification_id = center.json()["notifications"][0]["id"]
        marked = self.client.post(f"/notifications/{notification_id}/read")
        self.assertEqual(marked.status_code, 200)
        self.assertTrue(marked.json()["is_read"])

    def test_admin_manages_permissions_without_private_content(self) -> None:
        registered_member = self.client.post(
            "/auth/register",
            json={
                "email": "admin@example.com",
                "password": "password123",
                "display_name": "系统管理员",
            },
        )
        self.assertEqual(registered_member.json()["role"], "member")

        member_client = TestClient(self.app)
        registered_member = member_client.post(
            "/auth/register",
            json={
                "email": "member@example.com",
                "password": "password123",
                "display_name": "普通成员",
            },
        )
        member_id = registered_member.json()["id"]
        self.assertEqual(registered_member.json()["role"], "member")
        self.assertEqual(member_client.get("/admin/overview").status_code, 401)

        overview = self.client.get("/admin/overview")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["users"]["total"], 2)
        serialized = str(overview.json())
        self.assertNotIn("chat_messages", serialized)
        self.assertNotIn("travel_plans", serialized)
        self.assertNotIn("todos", serialized)

        updated = self.client.patch(
            f"/admin/users/{member_id}",
            json={"role": "knowledge_manager", "is_active": True},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["role"], "knowledge_manager")
        audit = self.client.get("/admin/audit-logs")
        self.assertEqual(audit.status_code, 200)
        self.assertEqual(audit.json()["logs"][0]["target"]["id"], member_id)
        self.assertNotIn("content", str(audit.json()).lower())

    def test_admin_can_manage_backend_admin_permissions(self) -> None:
        self.admin_auth.bootstrap(
            "second-admin@example.com",
            "temporary-admin-password-789",
            "二号管理员",
            can_manage_knowledge=False,
        )

        listed = self.client.get("/admin/admin-users")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["admins"]), 2)
        second_admin = next(
            item for item in listed.json()["admins"]
            if item["email"] == "second-admin@example.com"
        )
        self.assertFalse(second_admin["can_manage_knowledge"])

        updated = self.client.patch(
            f"/admin/admin-users/{second_admin['id']}",
            json={"can_manage_knowledge": True, "is_active": True},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.json()["can_manage_knowledge"])

        audit = self.client.get("/admin/audit-logs")
        self.assertEqual(audit.status_code, 200)
        self.assertEqual(audit.json()["logs"][0]["action"], "admin_permission_updated")
        self.assertEqual(audit.json()["logs"][0]["target"]["email"], "second-admin@example.com")

    def test_admin_cannot_disable_own_backend_account(self) -> None:
        me = self.client.get("/admin-auth/me").json()
        disabled = self.client.patch(
            f"/admin/admin-users/{me['id']}",
            json={"can_manage_knowledge": True, "is_active": False},
        )
        self.assertEqual(disabled.status_code, 400)
        self.assertIn("不能停用当前正在使用的后台管理员账号", disabled.json()["detail"])

    def test_admin_requires_second_factor_and_manages_sessions(self) -> None:
        short_wrong_password = self.client.post(
            "/admin-auth/login",
            json={"email": "system-admin@example.com", "password": "2329827114"},
        )
        self.assertEqual(short_wrong_password.status_code, 401)
        self.assertIsInstance(short_wrong_password.json()["detail"], str)

        second_admin_client = TestClient(self.app)
        password_step = second_admin_client.post(
            "/admin-auth/login",
            json={
                "email": "system-admin@example.com",
                "password": "independent-admin-password-456",
            },
        )
        self.assertEqual(password_step.status_code, 200)
        self.assertEqual(
            second_admin_client.get("/admin/overview").status_code,
            401,
        )
        verified = second_admin_client.post(
            "/admin-auth/mfa/verify",
            json={
                "challenge_token": password_step.json()["challenge_token"],
                "code": self.recovery_codes[0],
            },
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(second_admin_client.get("/admin/overview").status_code, 200)

        security = self.client.get("/admin-auth/security")
        self.assertEqual(security.status_code, 200)
        self.assertTrue(security.json()["authenticator_enabled"])
        self.assertEqual(security.json()["recovery_codes_remaining"], 7)
        self.assertEqual(security.json()["active_sessions"], 2)
        revoked = self.client.delete("/admin-auth/sessions/others")
        self.assertEqual(revoked.json()["revoked_count"], 1)
        self.assertEqual(second_admin_client.get("/admin/overview").status_code, 401)

    def test_email_otp_can_be_used_as_an_alternative_second_factor(self) -> None:
        with patch.object(SqliteAdminAuthService, "_send_email") as sender:
            sent = self.client.post(
                "/admin-auth/security/email/send",
                json={"email": "2185028055@qq.com"},
            )
            self.assertEqual(sent.status_code, 200)
            self.assertEqual(sender.call_args.args[0], "2185028055@qq.com")
            enable_code = sender.call_args.args[-2]
        enabled = self.client.post(
            "/admin-auth/security/email/enable",
            json={"code": enable_code},
        )
        self.assertEqual(enabled.status_code, 204)
        security = self.client.get("/admin-auth/security").json()
        self.assertTrue(security["email_recovery_enabled"])
        self.assertEqual(security["recovery_email"], "2185028055@qq.com")
        self.assertEqual(security["masked_email"], "21********@qq.com")

        self.client.post("/admin-auth/logout")
        password_step = self.client.post(
            "/admin-auth/login",
            json={
                "email": "system-admin@example.com",
                "password": "independent-admin-password-456",
            },
        ).json()
        self.assertTrue(password_step["email_recovery_available"])
        with patch.object(SqliteAdminAuthService, "_send_email") as sender:
            sent = self.client.post(
                "/admin-auth/email/login/send",
                json={"challenge_token": password_step["challenge_token"]},
            )
            self.assertEqual(sent.status_code, 200)
            self.assertEqual(sender.call_args.args[0], "2185028055@qq.com")
            login_code = sender.call_args.args[-2]
        recovered = self.client.post(
            "/admin-auth/email/login/verify",
            json={
                "challenge_token": password_step["challenge_token"],
                "code": login_code,
            },
        )
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(self.client.get("/admin/overview").status_code, 200)

    def test_admin_account_is_independent_and_deactivation_revokes_sessions(self) -> None:
        agent_user = self.client.post(
            "/auth/register",
            json={
                "email": "root@example.com",
                "password": "password123",
                "display_name": "管理员",
            },
        ).json()
        self.assertEqual(agent_user["role"], "member")
        agent_only_client = TestClient(self.app)
        self.assertEqual(
            agent_only_client.post(
                "/auth/login",
                json={"email": "root@example.com", "password": "password123"},
            ).status_code,
            200,
        )
        self.assertEqual(agent_only_client.get("/admin/overview").status_code, 401)

        member_client = TestClient(self.app)
        member = member_client.post(
            "/auth/register",
            json={
                "email": "disabled@example.com",
                "password": "password123",
                "display_name": "待停用成员",
            },
        ).json()
        disabled = self.client.patch(
            f"/admin/users/{member['id']}",
            json={"role": "member", "is_active": False},
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["is_active"])
        self.assertEqual(member_client.get("/auth/me").status_code, 401)

    def test_rejects_agent_request_without_login(self) -> None:
        response = self.client.post("/chat", json={"message": "你好"})

        self.assertEqual(response.status_code, 401)

    def test_uploads_lists_and_isolates_knowledge_documents(self) -> None:
        knowledge_user = self.client.post(
            "/auth/register",
            json={
                "email": "knowledge-a@example.com",
                "password": "password123",
                "display_name": "知识用户 A",
            },
        ).json()
        self.client.patch(
            f"/admin/users/{knowledge_user['id']}",
            json={"role": "knowledge_manager", "is_active": True},
        )
        self.client.post(
            "/auth/login",
            json={"email": "knowledge-a@example.com", "password": "password123"},
        )
        uploaded = self.client.post(
            "/knowledge/documents",
            data={"category": "公司制度"},
            files={
                "file": (
                    "报销规则.md",
                    "# 报销规则\n\n测试交通补贴为每天 66 元。".encode(),
                    "text/markdown",
                )
            },
        )

        self.assertEqual(uploaded.status_code, 201)
        self.assertEqual(uploaded.json()["category"], "公司制度")
        self.assertGreater(uploaded.json()["chunk_count"], 0)
        document_id = uploaded.json()["id"]
        self.assertEqual(
            len(self.client.get("/knowledge/documents").json()["documents"]),
            1,
        )

        self.client.post(
            "/auth/register",
            json={
                "email": "knowledge-b@example.com",
                "password": "password123",
                "display_name": "知识用户 B",
            },
        )
        self.assertEqual(
            self.client.get("/knowledge/documents").json()["documents"],
            [],
        )
        self.assertEqual(
            self.client.delete(f"/knowledge/documents/{document_id}").status_code,
            403,
        )

    def test_only_knowledge_roles_can_manage_documents(self) -> None:
        member = self.client.post(
            "/auth/register",
            json={
                "email": "knowledge-admin@example.com",
                "password": "password123",
                "display_name": "知识管理员",
            },
        ).json()
        self.assertEqual(member["role"], "member")

        member_client = TestClient(self.app)
        member = member_client.post(
            "/auth/register",
            json={
                "email": "knowledge-member@example.com",
                "password": "password123",
                "display_name": "普通成员",
            },
        ).json()
        upload = lambda: member_client.post(
            "/knowledge/documents",
            data={"category": "学习资料"},
            files={"file": ("资料.md", b"# Test\n\nKnowledge.", "text/markdown")},
        )

        rejected = upload()
        self.assertEqual(rejected.status_code, 403)
        self.assertIn("知识库管理员", rejected.json()["detail"])
        self.assertEqual(member_client.post("/knowledge/demo").status_code, 403)

        promoted = self.client.patch(
            f"/admin/users/{member['id']}",
            json={"role": "knowledge_manager", "is_active": True},
        )
        self.assertEqual(promoted.status_code, 200)
        self.assertEqual(promoted.json()["role"], "knowledge_manager")
        login = member_client.post(
            "/auth/login",
            json={
                "email": "knowledge-member@example.com",
                "password": "password123",
            },
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(upload().status_code, 201)

    def test_adds_demo_knowledge_only_once(self) -> None:
        demo_user = self.client.post(
            "/auth/register",
            json={
                "email": "demo@example.com",
                "password": "password123",
                "display_name": "演示用户",
            },
        ).json()
        self.client.patch(
            f"/admin/users/{demo_user['id']}",
            json={"role": "knowledge_manager", "is_active": True},
        )
        self.client.post(
            "/auth/login",
            json={"email": "demo@example.com", "password": "password123"},
        )

        first = self.client.post("/knowledge/demo")
        second = self.client.post("/knowledge/demo")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["created_count"], 1)
        self.assertEqual(second.json()["created_count"], 0)
        self.assertEqual(len(second.json()["documents"]), 1)

    def test_admin_manages_public_knowledge_base(self) -> None:
        uploaded = self.client.post(
            "/admin/knowledge/documents",
            data={"category": "公司制度"},
            files={
                "file": (
                    "远程办公制度.md",
                    "# 远程办公制度\n\n周三为团队协作日在办公室办公。".encode(),
                    "text/markdown",
                )
            },
        )

        self.assertEqual(uploaded.status_code, 201)
        self.assertEqual(uploaded.json()["category"], "公司制度")
        self.assertTrue(self.client.get("/admin-auth/me").json()["can_manage_knowledge"])

        listed = self.client.get("/admin/knowledge/documents")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["documents"]), 1)
        document_id = listed.json()["documents"][0]["id"]

        preview = self.client.get(f"/admin/knowledge/documents/{document_id}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("远程办公制度", preview.json()["content"])

        search = self.client.get("/admin/knowledge/search", params={"query": "团队协作日"})
        self.assertEqual(search.status_code, 200)
        self.assertGreaterEqual(len(search.json()["results"]), 1)

        removed = self.client.delete(f"/admin/knowledge/documents/{document_id}")
        self.assertEqual(removed.status_code, 204)
        self.assertEqual(
            self.client.get("/admin/knowledge/documents").json()["documents"],
            [],
        )

    def test_read_only_admin_cannot_modify_public_knowledge_base(self) -> None:
        self.admin_auth.bootstrap(
            "readonly-admin@example.com",
            "readonly-admin-password",
            "只读后台",
            can_manage_knowledge=False,
        )
        readonly_client = TestClient(self.app)
        password_step = readonly_client.post(
            "/admin-auth/login",
            json={
                "email": "readonly-admin@example.com",
                "password": "readonly-admin-password",
            },
        ).json()
        setup = readonly_client.post(
            "/admin-auth/mfa/setup",
            json={"challenge_token": password_step["challenge_token"]},
        ).json()
        activated = readonly_client.post(
            "/admin-auth/mfa/activate",
            json={
                "challenge_token": password_step["challenge_token"],
                "code": _totp(setup["secret"], int(time.time() // 30)),
                "new_password": "readonly-admin-password-2",
            },
        )
        self.assertEqual(activated.status_code, 200)
        self.assertFalse(activated.json()["user"]["can_manage_knowledge"])

        listed = readonly_client.get("/admin/knowledge/documents")
        self.assertEqual(listed.status_code, 200)

        uploaded = readonly_client.post(
            "/admin/knowledge/documents",
            data={"category": "公司制度"},
            files={"file": ("只读.md", b"# Read only", "text/markdown")},
        )
        self.assertEqual(uploaded.status_code, 403)
        self.assertIn("只有查看权限", uploaded.json()["detail"])

    def test_agent_user_cannot_access_admin_knowledge_base(self) -> None:
        member_client = TestClient(self.app)
        member_client.post(
            "/auth/register",
            json={
                "email": "frontend-user@example.com",
                "password": "password123",
                "display_name": "前台用户",
            },
        )

        self.assertEqual(
            member_client.get("/admin/knowledge/documents").status_code,
            401,
        )
        self.assertEqual(
            member_client.post("/admin/knowledge/demo").status_code,
            401,
        )

    def test_records_and_isolates_agent_runs(self) -> None:
        self.client.post(
            "/auth/register",
            json={
                "email": "trace@example.com",
                "password": "password123",
                "display_name": "追踪用户",
            },
        )
        self.client.post("/chat", json={"message": "记录这次运行"})

        runs = self.client.get("/observability/runs")

        self.assertEqual(runs.status_code, 200)
        self.assertEqual(len(runs.json()["runs"]), 1)
        run = runs.json()["runs"][0]
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["run_type"], "chat")
        detail = self.client.get(f"/observability/runs/{run['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["id"], run["id"])

    def test_runs_and_lists_isolated_evaluations(self) -> None:
        self.client.post(
            "/auth/register",
            json={
                "email": "eval@example.com",
                "password": "password123",
                "display_name": "评测用户",
            },
        )

        def fake_evaluation(store, run_id):
            store.add_case(
                run_id,
                "calculator_tool",
                "精确计算工具路由",
                "tool_routing",
                True,
                12,
                "调用计算器",
                "计算器调用成功",
            )
            return {"passed_cases": 1, "total_tokens": 20}

        with patch("hello_agent.api.run_core_evaluation", side_effect=fake_evaluation):
            response = self.client.post(
                "/evaluations/runs", json={"confirmed": True}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(response.json()["cases"][0]["status"], "passed")
        listed = self.client.get("/evaluations/runs")
        self.assertEqual(len(listed.json()["runs"]), 1)

    def test_manages_agentops_versions_and_queues_dataset_evaluation(self) -> None:
        self.client.post(
            "/auth/register",
            json={
                "email": "agentops@example.com",
                "password": "password123",
                "display_name": "AgentOps 用户",
            },
        )
        config = self.client.get("/agentops/config")
        self.assertEqual(config.status_code, 200)
        default_prompt = config.json()["prompts"][0]
        dataset = config.json()["datasets"][0]
        self.assertEqual(len(dataset["cases"]), 3)

        created = self.client.post(
            "/agentops/prompts",
            json={
                "name": "严格依据版",
                "content": "没有依据时必须明确说明未找到依据。",
                "change_note": "减少幻觉",
            },
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["version"], default_prompt["version"] + 1)
        activated = self.client.post(
            f"/agentops/prompts/{created.json()['id']}/activate"
        )
        self.assertEqual(activated.json()["status"], "active")

        launched = self.client.post(
            "/evaluations/runs",
            json={
                "confirmed": True,
                "prompt_version_id": created.json()["id"],
                "dataset_id": dataset["id"],
                "model": "deepseek-test",
            },
        )
        self.assertEqual(launched.status_code, 200)
        self.assertEqual(launched.json()["run"]["status"], "queued")
        self.assertEqual(launched.json()["task"]["task_type"], "evaluation_run")
        self.assertEqual(launched.json()["run"]["scorer"], "llm_judge")
        self.assertTrue(launched.json()["run"]["judge_enabled"])

    def test_exports_and_compares_evaluation_runs(self) -> None:
        self.client.post(
            "/auth/register",
            json={
                "email": "eval-export@example.com",
                "password": "password123",
                "display_name": "评测导出用户",
            },
        )

        outcomes = iter([True, False])

        def fake_evaluation(store, run_id):
            passed = next(outcomes)
            store.add_case(
                run_id,
                "knowledge_boundary",
                "知识边界",
                "safety",
                passed,
                18,
                "明确说明没有依据",
                "第一次：明确说明没有依据" if passed else "第二次：直接编造答案",
                score=100 if passed else 25,
                scorer="llm_judge",
                confidence="high" if passed else "low",
                failure_type=None if passed else "hallucination",
                failure_reason=None if passed else "编造了不存在的事实。",
            )
            return {"passed_cases": int(passed), "total_tokens": 20}

        with patch("hello_agent.api.run_core_evaluation", side_effect=fake_evaluation):
            first = self.client.post("/evaluations/runs", json={"confirmed": True})
            second = self.client.post("/evaluations/runs", json={"confirmed": True})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        compare = self.client.get(f"/evaluations/runs/{second.json()['id']}/compare?baseline_run_id={first.json()['id']}")
        self.assertEqual(compare.status_code, 200)
        self.assertEqual(compare.json()["summary"]["new_failures"], 1)

        export_json = self.client.get(f"/evaluations/runs/{second.json()['id']}/export?format=json")
        self.assertEqual(export_json.status_code, 200)
        self.assertIn("attachment;", export_json.headers["content-disposition"])
        self.assertIn('"cases"', export_json.text)

    def test_logout_invalidates_cookie(self) -> None:
        self.client.post(
            "/auth/register",
            json={
                "email": "user@example.com",
                "password": "password123",
                "display_name": "用户",
            },
        )

        response = self.client.post("/auth/logout")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get("/auth/me").status_code, 401)

    def test_users_cannot_access_each_others_agent_sessions(self) -> None:
        first_client = TestClient(self.app)
        second_client = TestClient(self.app)
        first_client.post(
            "/auth/register",
            json={
                "email": "first@example.com",
                "password": "password123",
                "display_name": "甲",
            },
        )
        session_id = first_client.post(
            "/chat", json={"message": "我的会话"}
        ).json()["session_id"]
        second_client.post(
            "/auth/register",
            json={
                "email": "second@example.com",
                "password": "password123",
                "display_name": "乙",
            },
        )

        response = second_client.post(
            "/chat",
            json={"session_id": session_id, "message": "尝试访问"},
        )

        self.assertEqual(response.status_code, 403)

    def test_pending_travel_task_is_isolated_by_account(self) -> None:
        first_client = TestClient(self.app)
        second_client = TestClient(self.app)
        first_user = first_client.post(
            "/auth/register",
            json={
                "email": "travel-first@example.com",
                "password": "password123",
                "display_name": "甲",
            },
        ).json()
        session_id = first_client.post(
            "/chat", json={"message": "创建旅行会话"}
        ).json()["session_id"]
        SqliteTravelStore(self.database_path, first_user["id"]).save(
            session_id,
            TravelPlan(
                status="needs_input",
                title="北京旅行需求",
                summary="等待补充。",
                destination="北京",
                missing_fields=["origin"],
                clarification_questions=["从哪里出发？"],
            ),
            user_input="我想去北京",
        )
        second_client.post(
            "/auth/register",
            json={
                "email": "travel-second@example.com",
                "password": "password123",
                "display_name": "乙",
            },
        )

        self.assertEqual(
            first_client.get(f"/travel-plans/pending/{session_id}").status_code,
            200,
        )
        self.assertEqual(
            second_client.get(f"/travel-plans/pending/{session_id}").status_code,
            403,
        )

    def test_lists_only_the_current_users_saved_conversations(self) -> None:
        first_client = TestClient(self.app)
        second_client = TestClient(self.app)
        first_client.post(
            "/auth/register",
            json={
                "email": "history-first@example.com",
                "password": "password123",
                "display_name": "甲",
            },
        )
        session_id = first_client.post(
            "/chat", json={"message": "创建历史会话"}
        ).json()["session_id"]
        SqliteConversationStore(self.database_path, session_id).append_turn(
            "什么是 RAG？",
            "RAG 是检索增强生成。",
        )
        second_client.post(
            "/auth/register",
            json={
                "email": "history-second@example.com",
                "password": "password123",
                "display_name": "乙",
            },
        )

        first_history = first_client.get("/sessions")
        second_history = second_client.get("/sessions")

        self.assertEqual(first_history.status_code, 200)
        self.assertEqual(len(first_history.json()["sessions"]), 1)
        saved = first_history.json()["sessions"][0]
        self.assertEqual(saved["session_id"], session_id)
        self.assertEqual(saved["title"], "什么是 RAG？")
        self.assertEqual(saved["preview"], "RAG 是检索增强生成。")
        self.assertEqual(saved["message_count"], 2)
        self.assertEqual(second_history.json()["sessions"], [])

    def test_todo_crud_is_shared_by_account_and_isolated_between_users(self) -> None:
        first_client = TestClient(self.app)
        second_client = TestClient(self.app)
        first_client.post(
            "/auth/register",
            json={
                "email": "todo-first@example.com",
                "password": "password123",
                "display_name": "甲",
            },
        )
        second_client.post(
            "/auth/register",
            json={
                "email": "todo-second@example.com",
                "password": "password123",
                "display_name": "乙",
            },
        )

        created = first_client.post("/todos", json={"title": "准备旅行"})
        todo_id = created.json()["id"]
        completed = first_client.patch(
            f"/todos/{todo_id}", json={"completed": True}
        )

        self.assertEqual(created.status_code, 201)
        self.assertTrue(completed.json()["completed"])
        self.assertEqual(len(first_client.get("/todos").json()), 1)
        self.assertEqual(second_client.get("/todos").json(), [])

        deleted = first_client.delete(f"/todos/{todo_id}")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(first_client.get("/todos").json(), [])

    def test_agent_plan_keeps_its_selected_todos_grouped(self) -> None:
        self.client.post(
            "/auth/register",
            json={
                "email": "plans@example.com",
                "password": "password123",
                "display_name": "计划用户",
            },
        )
        chat = self.client.post("/chat", json={"message": "创建会话"})
        session_id = chat.json()["session_id"]
        plan = {
            "title": "AI Agent 学习计划",
            "summary": "从基础到实践",
            "priority": "high",
            "steps": [
                {
                    "title": "学习 Python",
                    "description": "掌握 Python 基础",
                    "minutes": 120,
                },
                {
                    "title": "学习 Tool Calling",
                    "description": "完成工具调用练习",
                    "minutes": 90,
                },
            ],
        }

        synced = self.client.post(
            "/plans/todos",
            json={
                "session_id": session_id,
                "plan": plan,
                "selected_steps": plan["steps"],
                "confirmed": True,
            },
        )
        grouped = self.client.get("/todo-plans")

        self.assertEqual(synced.status_code, 200)
        self.assertEqual(grouped.status_code, 200)
        saved_plan = grouped.json()["plans"][0]
        self.assertEqual(saved_plan["title"], "AI Agent 学习计划")
        self.assertEqual(len(saved_plan["todos"]), 2)
        self.assertEqual(saved_plan["todos"][0]["minutes"], 120)


if __name__ == "__main__":
    unittest.main()
