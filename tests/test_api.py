"""FastAPI 接口测试，不调用真实模型。"""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from hello_agent.api import SessionManager, _default_session_factory, create_api
from hello_agent.app import AgentSession, ToolActivity
from hello_agent.auth import SqliteAuthService
from hello_agent.database import dispose_database_connections
from hello_agent.planning import GoalPlan, PlanStep
from hello_agent.travel import TravelActivity, TravelDay, TravelPlan


class FakeAgent:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.conversation: list[object] = []
        self.last_confirmation_requested = False
        self.last_action_cancelled = False
        self.last_sources: list[dict[str, object]] = []
        self.last_confidence = "none"
        self.last_grounding = "none"
        self.last_capture_grounding_failure = False
        self.model_usage = {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
        }
        self.trace_steps: list[dict[str, object]] = []
        self.model = "deepseek-v4-flash"

    def estimated_cost_total(self) -> float:
        return 0.0

    def models_label(self) -> str:
        return self.model

    def reset_trace(self) -> None:
        self.model_usage = {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
        }
        self.trace_steps = []
        self.last_sources = []
        self.last_confidence = "none"
        self.last_grounding = "none"
        self.last_capture_grounding_failure = False

    def grounding_payload(self) -> dict[str, object]:
        return {
            "sources": self.last_sources,
            "confidence": self.last_confidence,
            "grounding": self.last_grounding,
            "capture_grounding_failure": self.last_capture_grounding_failure,
        }

    def ask(self, message: str, approval_callback=None) -> str:
        self.messages.append(message)
        self.conversation.append({"role": "user", "content": message})
        if message == "完成任务 1":
            self.last_confirmation_requested = True
            approved = approval_callback("complete_todo", '{"id": 1}')
            self.last_action_cancelled = not approved
            answer = "任务已经完成。" if approved else "操作已取消。"
            self.conversation.append({"role": "assistant", "content": answer})
            return answer

        self.last_confirmation_requested = False
        self.last_action_cancelled = False
        answer = f"收到：{message}"
        self.conversation.append({"role": "assistant", "content": answer})
        return answer

    def ask_stream(
        self,
        message: str,
        approval_callback=None,
        include_tool_activity=False,
    ):
        if include_tool_activity and message == "查询天气":
            yield ToolActivity(
                call_id="call_weather",
                tool_name="get_weather",
                source="mcp",
                status="calling",
                message="正在通过 MCP Server 执行",
            )
        answer = self.ask(message, approval_callback)
        midpoint = max(1, len(answer) // 2)
        yield answer[:midpoint]
        yield answer[midpoint:]

    def create_plan(self, goal: str) -> GoalPlan:
        return GoalPlan(
            title=f"{goal}计划",
            summary="从一个小步骤开始。",
            priority="medium",
            steps=[
                PlanStep(
                    title="开始练习",
                    description="完成第一个可运行示例。",
                    minutes=30,
                )
            ],
        )

    def add_plan_todos(self, titles: list[str]):
        return [
            {"id": index, "title": title, "completed": False}
            for index, title in enumerate(titles, start=1)
        ]

    def create_travel_plan(
        self,
        request: str,
        previous_plan: TravelPlan | None = None,
    ) -> TravelPlan:
        if request == "我想去北京" and previous_plan is None:
            return TravelPlan(
                status="needs_input",
                title="北京旅行需求",
                summary="需要补充出发信息。",
                destination="北京",
                missing_fields=[
                    "origin", "start_date", "end_date", "travelers", "budget"
                ],
                clarification_questions=["请补充出发地、日期、人数和预算。"],
            )
        return TravelPlan(
            status="ready",
            title=f"{request}旅行计划",
            summary="价格为规划估算。",
            origin="上海",
            destination="北京",
            start_date="2026-10-01",
            end_date="2026-10-02",
            travelers=2,
            budget=5000,
            preferences=["历史文化"],
            days=[
                TravelDay(
                    day_number=1,
                    date="2026-10-01",
                    title="抵达北京",
                    activities=[
                        TravelActivity(
                            time="09:00",
                            title="参观故宫",
                            location="故宫博物院",
                            description="提前预约门票。",
                            estimated_cost=120,
                        )
                    ],
                )
            ],
        )

    def create_travel_preparation(self, travel_plan: TravelPlan) -> GoalPlan:
        if travel_plan.status != "confirmed":
            raise ValueError("请先确认旅行计划，再生成准备事项。")
        return GoalPlan(
            title=f"{travel_plan.title}出行准备",
            summary="完成预订与行李准备。",
            priority="high",
            steps=[
                PlanStep(
                    title="预订往返车票",
                    description="核对出行日期后完成预订。",
                    minutes=20,
                ),
                PlanStep(
                    title="整理证件和行李",
                    description="准备身份证和随身用品。",
                    minutes=30,
                ),
            ],
        )


class APITest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.temporary_directory.name, "agent.db")
        self.agents: dict[str, FakeAgent] = {}

        def factory(session_id: str, _user_id: str | None):
            agent = FakeAgent()
            self.agents[session_id] = agent
            return agent

        histories = {
            "11111111-1111-1111-1111-111111111111": [
                {"role": "user", "content": "我叫小明"},
                {"role": "assistant", "content": "好的，我记住了。"},
            ]
        }
        manager = SessionManager(
            session_factory=factory,
            history_loader=lambda session_id: histories.get(session_id, []),
        )
        self.client = TestClient(
            create_api(
                manager,
                auth_service=SqliteAuthService(Path(self.database_path)),
                auth_required=False,
            )
        )

    def tearDown(self) -> None:
        dispose_database_connections()
        self.temporary_directory.cleanup()

    def test_health(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_lists_discovered_mcp_tools(self) -> None:
        auth = SqliteAuthService(Path(self.database_path))
        client = TestClient(create_api(auth_service=auth))
        client.post(
            "/auth/register",
            json={
                "email": "mcp-list@example.com",
                "password": "password123",
                "display_name": "列表用户",
            },
        )
        fake_registry = Mock()
        fake_registry.server_infos.return_value = [
            {
                "id": None,
                "name": "Hello Agent 天气服务",
                "transport": "STDIO",
                "builtin": True,
                "enabled": True,
                "url": None,
                "status": "ready",
                "error_message": None,
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "查询天气",
                        "parameters": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                            "required": ["location"],
                        },
                    }
                ],
            },
            {
                "id": None,
                "name": "Hello Agent 本地知识库",
                "transport": "STDIO",
                "builtin": True,
                "enabled": True,
                "url": None,
                "status": "ready",
                "error_message": None,
                "tools": [
                    {
                        "name": "search_knowledge",
                        "description": "搜索本地资料",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            },
        ]
        with patch("hello_agent.api.build_user_mcp_registry", return_value=fake_registry):
            response = client.get("/mcp/tools")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["servers"][0]["transport"], "STDIO")
        self.assertEqual(body["servers"][0]["tools"][0]["name"], "get_weather")
        self.assertEqual(body["servers"][1]["tools"][0]["name"], "search_knowledge")

    def test_lists_and_loads_agent_skills(self) -> None:
        catalog = self.client.get("/skills")
        self.assertEqual(catalog.status_code, 200)
        names = {item["name"] for item in catalog.json()["skills"]}
        self.assertIn("precise-calculate", names)

        detail = self.client.get("/skills/precise-calculate")
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(body["name"], "precise-calculate")
        self.assertIn("calculate", body["body"])

        missing = self.client.get("/skills/not-a-real-skill")
        self.assertEqual(missing.status_code, 404)

    def test_default_session_factory_returns_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "agent.db")
            with patch.dict(os.environ, {"TODO_DATABASE_FILE": database_path}):
                agent = _default_session_factory(
                    "11111111-1111-1111-1111-111111111111"
                )

        self.assertIsInstance(agent, AgentSession)

    def test_allows_react_development_origin(self) -> None:
        response = self.client.options(
            "/chat",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "http://127.0.0.1:5173",
        )

    def test_chat_creates_and_reuses_session(self) -> None:
        first = self.client.post("/chat", json={"message": "我叫小明"})
        session_id = first.json()["session_id"]
        second = self.client.post(
            "/chat",
            json={"session_id": session_id, "message": "我叫什么"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.agents[session_id].messages, ["我叫小明", "我叫什么"])

    def test_chat_reports_required_approval(self) -> None:
        response = self.client.post("/chat", json={"message": "完成任务 1"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["approval_required"])
        self.assertEqual(response.json()["answer"], "操作已取消。")

    def test_chat_executes_approved_action(self) -> None:
        response = self.client.post(
            "/chat",
            json={"message": "完成任务 1", "approve": True},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["approval_required"])
        self.assertEqual(response.json()["answer"], "任务已经完成。")

    def test_chat_rejects_empty_message(self) -> None:
        response = self.client.post("/chat", json={"message": ""})

        self.assertEqual(response.status_code, 422)

    def test_loads_session_history(self) -> None:
        session_id = "11111111-1111-1111-1111-111111111111"

        response = self.client.get(f"/sessions/{session_id}/messages")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "session_id": session_id,
                "messages": [
                    {"role": "user", "content": "我叫小明"},
                    {"role": "assistant", "content": "好的，我记住了。"},
                ],
            },
        )

    def test_rejects_invalid_history_session_id(self) -> None:
        response = self.client.get("/sessions/not-a-uuid/messages")

        self.assertEqual(response.status_code, 422)

    def test_streams_chat_as_sse(self) -> None:
        response = self.client.post(
            "/chat/stream",
            json={"message": "你好"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertIn('"type": "session"', response.text)
        self.assertIn('"type": "delta"', response.text)
        self.assertIn('"type": "done"', response.text)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        answer = "".join(
            event["content"] for event in events if event["type"] == "delta"
        )
        self.assertEqual(answer, "收到：你好")

    def test_streams_tool_activity_as_sse(self) -> None:
        response = self.client.post(
            "/chat/stream",
            json={"message": "查询天气"},
        )

        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        activity = next(
            event for event in events if event["type"] == "tool_activity"
        )
        self.assertEqual(activity["tool_name"], "get_weather")
        self.assertEqual(activity["source"], "mcp")

    def test_creates_structured_plan(self) -> None:
        response = self.client.post(
            "/plans",
            json={"goal": "学习 Structured Output"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["plan"]["priority"], "medium")
        self.assertEqual(body["plan"]["steps"][0]["minutes"], 30)
        self.assertIn("session_id", body)

    def test_creates_and_persists_structured_travel_plan(self) -> None:
        response = self.client.post(
            "/travel-plans",
            json={"request": "上海到北京两日游"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["plan"]["status"], "ready")
        self.assertEqual(body["plan"]["days"][0]["activities"][0]["title"], "参观故宫")
        saved = self.client.get(f"/travel-plans/{body['travel_plan_id']}")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["destination"], "北京")
        self.assertEqual(len(self.client.get("/travel-plans").json()), 1)

        confirmed = self.client.post(
            f"/travel-plans/{body['travel_plan_id']}/confirm"
        )
        confirmed_again = self.client.post(
            f"/travel-plans/{body['travel_plan_id']}/confirm"
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["plan"]["status"], "confirmed")
        self.assertEqual(confirmed.json()["version"], 2)
        self.assertEqual(confirmed_again.json()["version"], 2)
        workflow_before_preparations = self.client.get(
            f"/travel-plans/{body['travel_plan_id']}/workflow"
        ).json()
        self.assertEqual(workflow_before_preparations["completed_count"], 3)
        self.assertEqual(
            workflow_before_preparations["stages"][3]["status"], "waiting"
        )
        checkpoints = self.client.get(
            f"/travel-plans/{body['travel_plan_id']}/checkpoints"
        ).json()
        self.assertEqual([item["status"] for item in checkpoints], ["ready", "confirmed"])

        preparations = self.client.post(
            f"/travel-plans/{body['travel_plan_id']}/preparations"
        )
        restored_preparations = self.client.post(
            f"/travel-plans/{body['travel_plan_id']}/preparations"
        )
        self.assertEqual(preparations.status_code, 200)
        self.assertFalse(preparations.json()["restored"])
        self.assertTrue(restored_preparations.json()["restored"])
        steps = preparations.json()["plan"]["steps"]
        self.assertEqual(steps[0]["title"], "预订往返车票")
        self.assertEqual(
            self.client.get(
                f"/travel-plans/{body['travel_plan_id']}/workflow"
            ).json()["completed_count"],
            4,
        )

        synced = self.client.post(
            f"/travel-plans/{body['travel_plan_id']}/todos",
            json={"selected_steps": steps, "confirmed": True},
        )
        synced_again = self.client.post(
            f"/travel-plans/{body['travel_plan_id']}/todos",
            json={"selected_steps": steps, "confirmed": True},
        )
        self.assertEqual(synced.status_code, 200)
        self.assertEqual(len(synced.json()["plan"]["todos"]), 2)
        self.assertEqual(
            synced.json()["plan"]["source_travel_plan_id"],
            body["travel_plan_id"],
        )
        self.assertEqual(
            synced_again.json()["plan"]["id"], synced.json()["plan"]["id"]
        )
        completed_workflow = self.client.get(
            f"/travel-plans/{body['travel_plan_id']}/workflow"
        ).json()
        self.assertEqual(completed_workflow["completed_count"], 5)
        self.assertFalse(completed_workflow["resumable"])
        summary = self.client.get("/travel-plans").json()[0]
        self.assertEqual(summary["workflow_completed_count"], 5)
        self.assertEqual(summary["workflow_total_count"], 5)

    def test_resumes_the_same_pending_travel_plan_from_a_checkpoint(self) -> None:
        first = self.client.post(
            "/travel-plans",
            json={"request": "我想去北京"},
        )
        first_body = first.json()
        session_id = first_body["session_id"]
        plan_id = first_body["travel_plan_id"]

        pending = self.client.get(f"/travel-plans/pending/{session_id}")
        resumed = self.client.post(
            "/travel-plans",
            json={
                "request": "上海出发，10月1日至2日，两个人，预算5000元",
                "session_id": session_id,
                "travel_plan_id": plan_id,
            },
        )

        self.assertEqual(pending.status_code, 200)
        self.assertEqual(pending.json()["travel_plan_id"], plan_id)
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["travel_plan_id"], plan_id)
        self.assertEqual(resumed.json()["version"], 2)
        self.assertEqual(resumed.json()["plan"]["status"], "ready")
        self.assertIsNone(
            self.client.get(f"/travel-plans/pending/{session_id}").json()
        )
        checkpoints = self.client.get(
            f"/travel-plans/{plan_id}/checkpoints"
        ).json()
        self.assertEqual([item["version"] for item in checkpoints], [1, 2])

    def test_syncs_confirmed_plan_steps_to_todos(self) -> None:
        plan_response = self.client.post(
            "/plans",
            json={"goal": "学习 Agent 工作流"},
        )
        plan_body = plan_response.json()
        session_id = plan_body["session_id"]

        response = self.client.post(
            "/plans/todos",
            json={
                "session_id": session_id,
                "plan": plan_body["plan"],
                "selected_steps": plan_body["plan"]["steps"],
                "confirmed": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["plan"]["title"], "学习 Agent 工作流计划")
        self.assertEqual(response.json()["plan"]["todos"][0]["title"], "开始练习")

    def test_rejects_unconfirmed_plan_sync(self) -> None:
        response = self.client.post(
            "/plans/todos",
            json={
                "session_id": "11111111-1111-1111-1111-111111111111",
                "plan": {
                    "title": "不会保存",
                    "summary": "未确认",
                    "priority": "medium",
                    "steps": [
                        {
                            "title": "不应写入",
                            "description": "未确认的步骤",
                            "minutes": 10,
                        }
                    ],
                },
                "selected_steps": [
                    {
                        "title": "不应写入",
                        "description": "未确认的步骤",
                        "minutes": 10,
                    }
                ],
                "confirmed": False,
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_updates_automation_settings_and_runs_daily_briefing(self) -> None:
        auth = SqliteAuthService(Path(self.database_path))
        client = TestClient(create_api(auth_service=auth))
        client.post(
            "/auth/register",
            json={
                "email": "automation@example.com",
                "password": "password123",
                "display_name": "自动化用户",
            },
        )

        settings = client.get("/automations/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertFalse(settings.json()["daily_todo_briefing_enabled"])

        updated = client.patch(
            "/automations/settings",
            json={"daily_todo_briefing_enabled": True, "daily_todo_briefing_hour": 7},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.json()["daily_todo_briefing_enabled"])
        self.assertEqual(updated.json()["daily_todo_briefing_hour"], 7)

        task_response = client.post("/automations/daily-todo-briefing/run")
        self.assertEqual(task_response.status_code, 200)
        task = task_response.json()["task"]
        self.assertEqual(task["task_type"], "daily_todo_briefing")
        self.assertEqual(task["payload"]["trigger"], "manual")


if __name__ == "__main__":
    unittest.main()
