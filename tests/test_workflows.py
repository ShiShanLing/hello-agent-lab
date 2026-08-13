"""工作流图校验、执行和持久化测试。"""

from pathlib import Path
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from hello_agent.api import create_api
from hello_agent.auth import SqliteAuthService
from hello_agent.database import SqliteWorkflowStore, dispose_database_connections
from hello_agent.task_worker import execute_background_task
from hello_agent.tools import TodoStore
from hello_agent.workflows import WorkflowDefinition, execute_workflow, stream_workflow


class WorkflowTest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    def definition(self) -> WorkflowDefinition:
        return WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {
                        "id": "input-1",
                        "type": "input",
                        "label": "输入",
                        "position": {"x": 0, "y": 0},
                    },
                    {
                        "id": "output-1",
                        "type": "output",
                        "label": "输出",
                        "position": {"x": 200, "y": 0},
                    },
                ],
                "edges": [
                    {"id": "edge-1", "source": "input-1", "target": "output-1"}
                ],
            }
        )

    def test_executes_simple_graph_and_records_steps(self) -> None:
        output, steps = execute_workflow(self.definition(), "你好工作流", [])
        self.assertEqual(output, "你好工作流")
        self.assertEqual([step["node_id"] for step in steps], ["input-1", "output-1"])
        self.assertTrue(all(step["status"] == "success" for step in steps))

    def test_rejects_cycles(self) -> None:
        with self.assertRaisesRegex(ValueError, "循环"):
            WorkflowDefinition.model_validate(
                {
                    "nodes": self.definition().model_dump()["nodes"],
                    "edges": [
                        {"id": "a", "source": "input-1", "target": "output-1"},
                        {"id": "b", "source": "output-1", "target": "input-1"},
                    ],
                }
            )

    def test_stream_pauses_for_approval_and_resumes(self) -> None:
        definition = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {"id": "approval", "type": "approval", "label": "人工确认", "position": {"x": 100, "y": 0}},
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 200, "y": 0}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "approval"},
                    {"id": "b", "source": "approval", "target": "output"},
                ],
            }
        )
        waiting = list(stream_workflow(definition, "待确认内容", []))
        self.assertEqual(waiting[-1]["type"], "approval_required")
        completed = list(stream_workflow(definition, "待确认内容", [], approved=True))
        self.assertEqual(completed[-1]["type"], "run_completed")
        self.assertEqual(completed[-1]["output"], "待确认内容")

    def test_condition_only_activates_matching_branch(self) -> None:
        definition = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {"id": "condition", "type": "condition", "label": "判断", "position": {"x": 100, "y": 0}, "config": {"contains": "旅行"}},
                    {"id": "yes", "type": "output", "label": "匹配输出", "position": {"x": 200, "y": 0}},
                    {"id": "no", "type": "output", "label": "未匹配输出", "position": {"x": 200, "y": 100}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "condition"},
                    {"id": "b", "source": "condition", "target": "yes", "source_handle": "true"},
                    {"id": "c", "source": "condition", "target": "no", "source_handle": "false"},
                ],
            }
        )
        events = list(stream_workflow(definition, "帮我规划旅行", []))
        statuses = {
            event["step"]["node_id"]: event["step"]["status"]
            for event in events if "step" in event
        }
        self.assertEqual(statuses["yes"], "success")
        self.assertEqual(statuses["no"], "skipped")

    def test_agent_dynamically_calls_configured_mcp_tool(self) -> None:
        tool_call = SimpleNamespace(
            id="call-weather",
            function=SimpleNamespace(
                name="get_weather",
                arguments='{"location":"上海","days":1}',
            ),
        )
        responses = iter(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))]
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="上海天气适合出行。", tool_calls=[]))]
                ),
            ]
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: next(responses))
            )
        )

        class FakeRegistry:
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "查询天气",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]

            def call_tool(self, name: str, arguments: str) -> str:
                self.called = (name, arguments)
                return '{"weather":"晴"}'

        definition = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {"id": "agent", "type": "agent", "label": "Agent", "position": {"x": 100, "y": 0}, "config": {"tools": ["get_weather"]}},
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 200, "y": 0}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "agent"},
                    {"id": "b", "source": "agent", "target": "output"},
                ],
            }
        )
        registry = FakeRegistry()
        with patch("hello_agent.workflows.create_client", return_value=fake_client):
            output, steps = execute_workflow(
                definition, "上海天气如何", [], mcp_client=registry  # type: ignore[arg-type]
            )
        self.assertEqual(output, "上海天气适合出行。")
        self.assertEqual(registry.called[0], "get_weather")
        self.assertIn("调用 1 个工具", steps[1]["summary"])

    def test_agent_injects_selected_skill_instructions(self) -> None:
        captured: list[dict[str, object]] = []

        def create(**kwargs: object) -> SimpleNamespace:
            captured.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="请使用计算器，不要心算。", tool_calls=[])
                    )
                ]
            )

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        definition = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {
                        "id": "agent",
                        "type": "agent",
                        "label": "Agent",
                        "position": {"x": 100, "y": 0},
                        "config": {"tools": [], "skills": ["precise-calculate"]},
                    },
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 200, "y": 0}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "agent"},
                    {"id": "b", "source": "agent", "target": "output"},
                ],
            }
        )
        with patch("hello_agent.workflows.create_client", return_value=fake_client):
            output, steps = execute_workflow(definition, "请精确计算 12*3", [], mcp_client=SimpleNamespace(tools=[]))  # type: ignore[arg-type]

        system = str(captured[0]["messages"][0]["content"])
        self.assertEqual(output, "请使用计算器，不要心算。")
        self.assertIn("precise-calculate", system)
        self.assertIn("不要心算", system)
        self.assertEqual(steps[1]["output"]["skills_loaded"], ["precise-calculate"])
        self.assertIn("加载 1 个 Skill", steps[1]["summary"])

    def test_agent_rejects_unknown_skill(self) -> None:
        definition = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {
                        "id": "agent",
                        "type": "agent",
                        "label": "Agent",
                        "position": {"x": 100, "y": 0},
                        "config": {"tools": [], "skills": ["not-a-real-skill"]},
                    },
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 200, "y": 0}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "agent"},
                    {"id": "b", "source": "agent", "target": "output"},
                ],
            }
        )
        with self.assertRaisesRegex(Exception, "无效 Skill"):
            execute_workflow(definition, "测试", [], mcp_client=SimpleNamespace(tools=[]))  # type: ignore[arg-type]

    def test_todo_node_writes_local_task(self) -> None:
        store = TodoStore()
        definition = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {"id": "todo", "type": "todo", "label": "添加 Todo", "position": {"x": 100, "y": 0}},
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 200, "y": 0}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "todo"},
                    {"id": "b", "source": "todo", "target": "output"},
                ],
            }
        )
        output, steps = execute_workflow(
            definition, "学习工作流 Todo", [], todo_store=store
        )
        self.assertEqual(store.list_all()[0]["title"], "学习工作流 Todo")
        self.assertIn("已添加任务", steps[1]["summary"])
        self.assertIn("学习工作流 Todo", output)

    def test_agent_can_call_add_todo_local_tool(self) -> None:
        store = TodoStore()
        tool_call = SimpleNamespace(
            id="call-todo",
            function=SimpleNamespace(
                name="add_todo",
                arguments='{"title":"学习 Skills"}',
            ),
        )
        responses = iter(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))]
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="已经记下学习 Skills。", tool_calls=[]))]
                ),
            ]
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: next(responses))
            )
        )
        definition = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {
                        "id": "agent",
                        "type": "agent",
                        "label": "Agent",
                        "position": {"x": 100, "y": 0},
                        "config": {"tools": ["add_todo"]},
                    },
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 200, "y": 0}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "agent"},
                    {"id": "b", "source": "agent", "target": "output"},
                ],
            }
        )
        with patch("hello_agent.workflows.create_client", return_value=fake_client):
            output, steps = execute_workflow(
                definition,
                "帮我添加任务",
                [],
                mcp_client=SimpleNamespace(tools=[]),  # type: ignore[arg-type]
                todo_store=store,
            )
        self.assertEqual(output, "已经记下学习 Skills。")
        self.assertEqual(store.list_all()[0]["title"], "学习 Skills")
        self.assertIn("add_todo", steps[1]["output"]["tools_used"])

    def test_saves_definition_and_run_by_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteWorkflowStore(Path(directory) / "agent.db", "user-1")
            saved = store.save(
                "测试工作流", "输入直达输出", self.definition().model_dump()
            )
            run_id = store.create_run(str(saved["id"]), "测试输入")
            run = store.finish_run(run_id, "success", "测试输入", [])
            self.assertEqual(store.list()[0]["name"], "测试工作流")
            self.assertEqual(run["status"], "success")
            self.assertEqual(store.list_runs(str(saved["id"]))[0]["output_text"], "测试输入")

    def test_workflow_api_saves_and_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "agent.db"
            client = TestClient(
                create_api(auth_service=SqliteAuthService(database_path))
            )
            register = client.post(
                "/auth/register",
                json={
                    "email": "workflow@example.com",
                    "password": "workflow-pass-123",
                    "display_name": "工作流测试",
                },
            )
            self.assertEqual(register.status_code, 201)
            saved = client.post(
                "/workflows",
                json={
                    "name": "API 工作流",
                    "description": "接口测试",
                    "definition": self.definition().model_dump(),
                },
            )
            self.assertEqual(saved.status_code, 200)
            workflow_id = saved.json()["id"]
            result = client.post(
                f"/workflows/{workflow_id}/runs",
                json={"input": "API 测试输入"},
            )
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["run"]["status"], "queued")
            self.assertEqual(result.json()["task"]["task_type"], "workflow_run")
            execute_background_task(
                str(database_path),
                result.json()["task"]["id"],
                register.json()["id"],
            )
            completed = client.get(f"/workflow-runs/{result.json()['run']['id']}")
            self.assertEqual(completed.status_code, 200)
            self.assertEqual(completed.json()["status"], "success")
            self.assertEqual(completed.json()["output_text"], "API 测试输入")

    def test_stream_api_waits_and_continues_after_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(
                create_api(
                    auth_service=SqliteAuthService(Path(directory) / "agent.db")
                )
            )
            client.post(
                "/auth/register",
                json={
                    "email": "approval@example.com",
                    "password": "workflow-pass-123",
                    "display_name": "审批测试",
                },
            )
            definition = {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {"id": "approval", "type": "approval", "label": "确认", "position": {"x": 100, "y": 0}},
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 200, "y": 0}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "approval"},
                    {"id": "b", "source": "approval", "target": "output"},
                ],
            }
            saved = client.post(
                "/workflows",
                json={"name": "审批流", "description": "", "definition": definition},
            ).json()
            stream = client.post(
                f"/workflows/{saved['id']}/runs/stream",
                json={"input": "确认后输出"},
            )
            self.assertIn('"type": "approval_required"', stream.text)
            run_marker = next(
                line for line in stream.text.splitlines()
                if '"type": "run"' in line
            )
            import json
            run_id = json.loads(run_marker.removeprefix("data: "))["run_id"]
            resumed = client.post(
                f"/workflow-runs/{run_id}/approve/stream",
                json={"confirmed": True},
            )
            self.assertIn('"type": "run_completed"', resumed.text)
            self.assertIn("确认后输出", resumed.text)

    def _approval_definition(self) -> dict[str, object]:
        return {
            "nodes": [
                {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                {"id": "approval", "type": "approval", "label": "确认", "position": {"x": 100, "y": 0}},
                {"id": "output", "type": "output", "label": "输出", "position": {"x": 200, "y": 0}},
            ],
            "edges": [
                {"id": "a", "source": "input", "target": "approval"},
                {"id": "b", "source": "approval", "target": "output"},
            ],
        }

    def test_async_run_waits_and_continues_after_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "agent.db"
            client = TestClient(
                create_api(auth_service=SqliteAuthService(database_path))
            )
            user = client.post(
                "/auth/register",
                json={
                    "email": "async-approval@example.com",
                    "password": "workflow-pass-123",
                    "display_name": "异步审批",
                },
            ).json()
            saved = client.post(
                "/workflows",
                json={"name": "异步审批流", "description": "", "definition": self._approval_definition()},
            ).json()
            launched = client.post(
                f"/workflows/{saved['id']}/runs",
                json={"input": "确认后输出"},
            )
            self.assertEqual(launched.status_code, 200)
            task_id = launched.json()["task"]["id"]
            run_id = launched.json()["run"]["id"]
            waiting = execute_background_task(str(database_path), task_id, user["id"])
            self.assertEqual(waiting["status"], "waiting")
            paused = client.get(f"/workflow-runs/{run_id}")
            self.assertEqual(paused.json()["status"], "waiting_approval")
            center = client.get("/tasks")
            self.assertEqual(center.json()["tasks"][0]["status"], "waiting")
            resumed = client.post(
                f"/workflow-runs/{run_id}/approve",
                json={"confirmed": True},
            )
            self.assertEqual(resumed.status_code, 200)
            self.assertEqual(resumed.json()["task"]["status"], "queued")
            completed = execute_background_task(str(database_path), task_id, user["id"])
            self.assertEqual(completed["run_id"], run_id)
            finished = client.get(f"/workflow-runs/{run_id}")
            self.assertEqual(finished.json()["status"], "success")
            self.assertEqual(finished.json()["output_text"], "确认后输出")

    def test_async_run_can_be_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(
                create_api(
                    auth_service=SqliteAuthService(Path(directory) / "agent.db")
                )
            )
            client.post(
                "/auth/register",
                json={
                    "email": "cancel-run@example.com",
                    "password": "workflow-pass-123",
                    "display_name": "取消运行",
                },
            )
            saved = client.post(
                "/workflows",
                json={"name": "取消流", "description": "", "definition": self.definition().model_dump()},
            ).json()
            launched = client.post(
                f"/workflows/{saved['id']}/runs",
                json={"input": "还没开始"},
            )
            cancelled = client.post(f"/tasks/{launched.json()['task']['id']}/cancel")
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json()["status"], "cancelled")
            run = client.get(f"/workflow-runs/{launched.json()['run']['id']}")
            self.assertEqual(run.json()["status"], "cancelled")

    def test_transform_template_and_variable_mapping(self) -> None:
        definition = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}, "config": {"save_as": "city"}},
                    {"id": "transform", "type": "transform", "label": "转换", "position": {"x": 120, "y": 0}, "config": {"mode": "template", "template": "去{{vars.city}}出差"}},
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 240, "y": 0}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "transform"},
                    {"id": "b", "source": "transform", "target": "output"},
                ],
            }
        )
        output, steps = execute_workflow(definition, "杭州", [])
        self.assertEqual(output, "去杭州出差")
        self.assertEqual(steps[1]["output"], "去杭州出差")

    def test_foreach_splits_lines_and_joins(self) -> None:
        definition = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {"id": "foreach", "type": "foreach", "label": "批处理", "position": {"x": 120, "y": 0}, "config": {"template": "- {{item}}", "join": "\n"}},
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 240, "y": 0}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "foreach"},
                    {"id": "b", "source": "foreach", "target": "output"},
                ],
            }
        )
        output, _steps = execute_workflow(definition, "苹果\n香蕉\n梨", [])
        self.assertEqual(output, "- 苹果\n- 香蕉\n- 梨")

    def test_http_node_calls_public_api_and_rejects_private_hosts(self) -> None:
        definition = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {"id": "http", "type": "http", "label": "请求", "position": {"x": 120, "y": 0}, "config": {"url": "https://example.com/echo?q={{upstream}}", "method": "GET"}},
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 240, "y": 0}, "config": {"map_from": "json.echo"}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "http"},
                    {"id": "b", "source": "http", "target": "output"},
                ],
            }
        )
        response = SimpleNamespace(
            status_code=200,
            text='{"echo":"上海"}',
            content='{"echo":"上海"}'.encode(),
            headers={"content-type": "application/json"},
        )
        with patch("hello_agent.workflows.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("1.1.1.1", 443))]):
            with patch("hello_agent.workflows._http_request", return_value=response):
                output, steps = execute_workflow(definition, "上海", [])
        self.assertEqual(output, "上海")
        self.assertEqual(steps[1]["output"]["status_code"], 200)

        blocked = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {"id": "http", "type": "http", "label": "请求", "position": {"x": 120, "y": 0}, "config": {"url": "http://127.0.0.1/secret"}},
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 240, "y": 0}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "http"},
                    {"id": "b", "source": "http", "target": "output"},
                ],
            }
        )
        with self.assertRaisesRegex(Exception, "内网|本机|保留"):
            execute_workflow(blocked, "x", [])

    def test_subworkflow_node_runs_saved_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteWorkflowStore(Path(directory) / "agent.db", "user-1")
            child = store.save(
                "子流程",
                "把输入加上前缀",
                WorkflowDefinition.model_validate(
                    {
                        "nodes": [
                            {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                            {"id": "transform", "type": "transform", "label": "转换", "position": {"x": 120, "y": 0}, "config": {"mode": "template", "template": "已处理：{{upstream}}"}},
                            {"id": "output", "type": "output", "label": "输出", "position": {"x": 240, "y": 0}},
                        ],
                        "edges": [
                            {"id": "a", "source": "input", "target": "transform"},
                            {"id": "b", "source": "transform", "target": "output"},
                        ],
                    }
                ).model_dump(),
            )
            parent = WorkflowDefinition.model_validate(
                {
                    "nodes": [
                        {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                        {"id": "child", "type": "subworkflow", "label": "调用子流程", "position": {"x": 120, "y": 0}, "config": {"workflow_id": child["id"]}},
                        {"id": "output", "type": "output", "label": "输出", "position": {"x": 240, "y": 0}},
                    ],
                    "edges": [
                        {"id": "a", "source": "input", "target": "child"},
                        {"id": "b", "source": "child", "target": "output"},
                    ],
                }
            )
            output, steps = execute_workflow(
                parent, "资料", [], workflow_store=store, current_workflow_id="parent"
            )
            self.assertEqual(output, "已处理：资料")
            self.assertEqual(steps[1]["output"]["workflow"], "子流程")

    def test_custom_mcp_arguments_json(self) -> None:
        registry = SimpleNamespace(
            call_tool=lambda name, arguments: json.dumps(
                {"tool": name, "arguments": json.loads(arguments)}, ensure_ascii=False
            )
        )
        definition = WorkflowDefinition.model_validate(
            {
                "nodes": [
                    {"id": "input", "type": "input", "label": "输入", "position": {"x": 0, "y": 0}},
                    {"id": "mcp", "type": "mcp", "label": "MCP", "position": {"x": 120, "y": 0}, "config": {"tool": "search_knowledge", "arguments_json": '{"query":"{{upstream}}","limit":2}'}},
                    {"id": "output", "type": "output", "label": "输出", "position": {"x": 240, "y": 0}},
                ],
                "edges": [
                    {"id": "a", "source": "input", "target": "mcp"},
                    {"id": "b", "source": "mcp", "target": "output"},
                ],
            }
        )
        output, steps = execute_workflow(
            definition, "报销制度", [], mcp_client=registry  # type: ignore[arg-type]
        )
        self.assertEqual(steps[1]["output"]["arguments"]["query"], "报销制度")
        self.assertEqual(steps[1]["output"]["arguments"]["limit"], 2)
        self.assertIn("报销制度", output)


if __name__ == "__main__":
    unittest.main()
