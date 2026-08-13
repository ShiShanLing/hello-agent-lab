"""多 Agent 协作：工具执行与审核打回。"""

import unittest
from types import SimpleNamespace

from hello_agent.app import AgentSession
from hello_agent.collaboration import (
    collaboration_tool_catalog,
    resume_collaboration,
    run_collaboration,
)
from hello_agent.openapi_registry import OpenApiToolRegistry
from hello_agent.tools import TodoStore
from tests.test_app import FakeClient


def _text(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=None)


def _tool(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id=call_id,
                function=SimpleNamespace(name=name, arguments=arguments),
            )
        ],
    )


class CollaborationTest(unittest.TestCase):
    def test_executor_uses_tools_and_reviewer_approves(self) -> None:
        client = FakeClient(
            messages=[
                _text("1. 先用计算器求值"),
                _tool("call_calc", "calculate", '{"expression":"12 + 8"}'),
                _text("计算结果是 20。"),
                _tool(
                    "call_approve",
                    "approve_delivery",
                    '{"answer":"12 加 8 等于 20。"}',
                ),
            ]
        )
        store = TodoStore()
        session = AgentSession(
            client=client,
            todo_store=store,
            auto_search_knowledge=False,
        )
        before = len(session.messages)

        events = list(run_collaboration("请精确计算 12+8", session, pause_after_plan=False))
        statuses = [(event.agent, event.status, event.tool_name) for event in events]

        self.assertIn(("executor", "tool_calling", "calculate"), statuses)
        self.assertIn(("executor", "tool_completed", "calculate"), statuses)
        self.assertEqual(events[-1].agent, "reviewer")
        self.assertEqual(events[-1].status, "completed")
        self.assertEqual(events[-1].content, "12 加 8 等于 20。")
        self.assertEqual(len(session.messages), before)
        planner_prompt = client.chat.completions.requests[0]["messages"][1]["content"]
        self.assertIn("calculate", planner_prompt)
        self.assertTrue(
            any(
                tool["function"]["name"] == "calculate"
                for tool in client.chat.completions.requests[1]["tools"]
            )
        )
        self.assertNotIn(
            "complete_todo",
            [
                tool["function"]["name"]
                for tool in session.collaboration_tools()
                if isinstance(tool.get("function"), dict)
            ],
        )

    def test_executor_can_add_todo_but_not_complete(self) -> None:
        client = FakeClient(
            messages=[
                _text("1. 用 add_todo 记录任务"),
                _tool("call_add", "add_todo", '{"title":"学习协作"}'),
                _text("已添加待办。"),
                _tool(
                    "call_approve",
                    "approve_delivery",
                    '{"answer":"已帮你添加待办：学习协作。"}',
                ),
            ]
        )
        store = TodoStore()
        session = AgentSession(
            client=client,
            todo_store=store,
            auto_search_knowledge=False,
        )
        events = list(run_collaboration("帮我记一个待办：学习协作", session, pause_after_plan=False))
        self.assertIn(
            ("executor", "tool_completed", "add_todo"),
            [(event.agent, event.status, event.tool_name) for event in events],
        )
        self.assertEqual(len(store.list_all()), 1)
        self.assertEqual(store.list_all()[0]["title"], "学习协作")

        blocked = session.collaboration_tools()
        names = {
            str(tool["function"]["name"])
            for tool in blocked
            if isinstance(tool.get("function"), dict)
        }
        self.assertIn("add_todo", names)
        self.assertNotIn("complete_todo", names)

    def test_confirmation_required_tools_are_cancelled_in_collaboration(self) -> None:
        client = FakeClient(
            messages=[
                _text("尝试完成任务"),
                _tool("call_done", "complete_todo", '{"todo_id":1}'),
                _text("无法在协作中完成，需要用户确认。"),
                _tool(
                    "call_approve",
                    "approve_delivery",
                    '{"answer":"完成任务需要你在对话中确认。"}',
                ),
            ]
        )
        store = TodoStore()
        store.add("临时任务")
        session = AgentSession(
            client=client,
            todo_store=store,
            auto_search_knowledge=False,
        )
        # Force complete_todo into ephemeral tools path by mocking collaboration_tools.
        session.collaboration_tools = lambda: [  # type: ignore[method-assign]
            {
                "type": "function",
                "function": {
                    "name": "complete_todo",
                    "description": "完成任务",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        events = list(run_collaboration("完成任务 1", session, pause_after_plan=False))
        self.assertIn(
            ("executor", "tool_completed", "complete_todo"),
            [(event.agent, event.status, event.tool_name) for event in events],
        )
        self.assertFalse(store.list_all()[0]["completed"])

    def test_reviewer_can_send_work_back_to_executor(self) -> None:
        client = FakeClient(
            messages=[
                _text("先计算再给结论"),
                _text("大约是 20。"),
                _tool(
                    "call_revise",
                    "request_revision",
                    '{"feedback":"必须调用计算器，不要估算"}',
                ),
                _tool("call_calc", "calculate", '{"expression":"12 + 8"}'),
                _text("精确结果是 20。"),
                _tool(
                    "call_approve",
                    "approve_delivery",
                    '{"answer":"精确结果是 20。"}',
                ),
            ]
        )
        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
            auto_search_knowledge=False,
        )

        events = list(run_collaboration("请精确计算 12+8", session, pause_after_plan=False))
        reviewer_events = [event for event in events if event.agent == "reviewer"]
        executor_stages = [
            event.stage_id
            for event in events
            if event.agent == "executor" and event.status == "running"
        ]

        self.assertTrue(
            any(event.status == "revision" for event in reviewer_events)
        )
        self.assertEqual(executor_stages, ["executor-1", "executor-2"])
        self.assertEqual(events[-1].content, "精确结果是 20。")
        second_executor_prompt = client.chat.completions.requests[3]["messages"][1][
            "content"
        ]
        self.assertIn("必须调用计算器", second_executor_prompt)

    def test_openapi_write_tools_excluded_from_collaboration(self) -> None:
        registry = OpenApiToolRegistry(
            [
                {
                    "id": "src-1",
                    "name": "Demo",
                    "base_url": "https://example.com",
                    "enabled": True,
                    "selected_operations": ["createPet"],
                    "spec": {
                        "openapi": "3.0.0",
                        "info": {"title": "Demo", "version": "1"},
                        "servers": [{"url": "https://example.com"}],
                        "paths": {
                            "/pets": {
                                "post": {
                                    "operationId": "createPet",
                                    "summary": "创建",
                                    "requestBody": {
                                        "content": {
                                            "application/json": {
                                                "schema": {"type": "object"}
                                            }
                                        }
                                    },
                                }
                            }
                        },
                    },
                }
            ]
        )
        session = AgentSession(
            todo_store=TodoStore(),
            openapi_registry=registry,
            auto_search_knowledge=False,
        )
        names = {
            str(tool["function"]["name"])
            for tool in session.collaboration_tools()
            if isinstance(tool.get("function"), dict)
        }
        self.assertNotIn("createpet", names)
        catalog = collaboration_tool_catalog(session.collaboration_tools())
        self.assertIn("calculate", catalog)

    def test_collaboration_pauses_after_plan_for_hitl(self) -> None:
        client = FakeClient(messages=[_text("1. 先计算\n2. 再总结")])
        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
            auto_search_knowledge=False,
        )
        events = list(
            run_collaboration(
                "请精确计算 12+8",
                session,
                pause_after_plan=True,
                collaboration_id="collab-1",
            )
        )
        self.assertEqual(events[-1].status, "waiting_approval")
        self.assertEqual(events[-1].collaboration_id, "collab-1")
        self.assertIn("先计算", events[-1].content or "")
        self.assertFalse(
            any(event.agent == "executor" for event in events)
        )

    def test_resume_collaboration_runs_executor_and_reviewer(self) -> None:
        client = FakeClient(
            messages=[
                _tool("call_calc", "calculate", '{"expression":"12 + 8"}'),
                _text("计算结果是 20。"),
                _tool(
                    "call_approve",
                    "approve_delivery",
                    '{"answer":"12 加 8 等于 20。"}',
                ),
            ]
        )
        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
            auto_search_knowledge=False,
        )
        events = list(
            resume_collaboration(
                "请精确计算 12+8",
                "1. 使用 calculate\n2. 汇报结果",
                session,
            )
        )
        self.assertIn(
            ("executor", "tool_completed", "calculate"),
            [(event.agent, event.status, event.tool_name) for event in events],
        )
        self.assertEqual(events[-1].content, "12 加 8 等于 20。")


if __name__ == "__main__":
    unittest.main()
