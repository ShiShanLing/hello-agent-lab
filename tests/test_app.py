"""不调用真实 API 的基础测试。"""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hello_agent.app import (
    AgentSession,
    DEFAULT_MODEL,
    MissingAPIKeyError,
    ToolActivity,
    ask_ai,
)
from hello_agent.database import (
    SqliteConversationStore,
    SqliteTodoStore,
    dispose_database_connections,
)
from hello_agent.tools import TodoStore


class FakeCompletions:
    def __init__(self, messages=None) -> None:
        self.last_request = None
        self.requests = []
        self.messages = messages or [
            SimpleNamespace(content="这是模拟的 DeepSeek 回答。", tool_calls=None)
        ]

    def create(self, **kwargs):
        self.last_request = kwargs
        # 保存当次请求快照，避免后续追加历史消息改变旧记录。
        self.requests.append(copy.deepcopy(kwargs))
        message = self.messages[len(self.requests) - 1]
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeChat:
    def __init__(self, messages=None) -> None:
        self.completions = FakeCompletions(messages)


class FakeClient:
    def __init__(self, messages=None) -> None:
        self.chat = FakeChat(messages)


class FakeMCPClient:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查询天气",
                "parameters": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            },
        }
    ]
    tool_names = {"get_weather"}

    def __init__(self) -> None:
        self.calls = []

    def call_tool(self, name: str, arguments: str) -> str:
        self.calls.append((name, arguments))
        return '{"weather":{"location":{"name":"上海"}}}'


class FakeKnowledgeMCPClient:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "搜索本地知识库",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    tool_names = {"search_knowledge"}

    def __init__(self, has_results: bool = True) -> None:
        self.calls = []
        self.has_results = has_results

    def call_tool(self, name: str, arguments: str) -> str:
        self.calls.append((name, arguments))
        results = (
            [
                {
                    "source": "agent.md",
                    "chunk": 1,
                    "content": "RAG 会先检索资料，再让模型依据资料回答。",
                    "score": 8.0,
                }
            ]
            if self.has_results
            else []
        )
        return json.dumps(
            {
                "results": results,
                "confidence": "high" if self.has_results else "low",
                "document_count": 1 if self.has_results else 0,
            },
            ensure_ascii=False,
        )


def stream_chunk(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=content,
                    reasoning_content=None,
                    tool_calls=tool_calls,
                )
            )
        ]
    )


class FakeStreamingCompletions:
    def __init__(self, rounds) -> None:
        self.rounds = rounds
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return iter(self.rounds[len(self.requests) - 1])


class FakeStreamingClient:
    def __init__(self, rounds) -> None:
        self.chat = SimpleNamespace(completions=FakeStreamingCompletions(rounds))


class AskAITest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    def test_returns_model_output(self) -> None:
        client = FakeClient()

        answer = ask_ai("什么是函数？", client=client, todo_store=TodoStore())

        request = client.chat.completions.last_request
        self.assertEqual(answer, "这是模拟的 DeepSeek 回答。")
        self.assertEqual(request["messages"][1]["content"], "什么是函数？")
        self.assertEqual(request["model"], DEFAULT_MODEL)

    def test_rejects_empty_question(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能为空"):
            ask_ai("   ", client=FakeClient(), todo_store=TodoStore())

    def test_requires_api_key_for_real_client(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingAPIKeyError):
                ask_ai("你好", todo_store=TodoStore())

    def test_model_can_be_changed_with_environment_variable(self) -> None:
        client = FakeClient()

        with patch.dict(os.environ, {"DEEPSEEK_MODEL": "another-model"}):
            ask_ai("你好", client=client, todo_store=TodoStore())

        self.assertEqual(
            client.chat.completions.last_request["model"], "another-model"
        )

    def test_executes_calculator_tool_and_returns_final_answer(self) -> None:
        tool_call = SimpleNamespace(
            id="call_123",
            function=SimpleNamespace(
                name="calculate",
                arguments='{"expression": "(12 + 8) * 3"}',
            ),
        )
        client = FakeClient(
            messages=[
                SimpleNamespace(content=None, tool_calls=[tool_call]),
                SimpleNamespace(content="计算结果是 60。", tool_calls=None),
            ]
        )

        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
        )
        answer = session.ask("(12 + 8) * 3 等于多少？")

        requests = client.chat.completions.requests
        self.assertEqual(answer, "计算结果是 60。")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["messages"][-1]["role"], "tool")
        self.assertIn('"result": "60"', requests[1]["messages"][-1]["content"])
        self.assertEqual(
            [step["step_type"] for step in session.trace_steps],
            ["model", "tool", "model"],
        )

    def test_injects_skill_catalog_and_load_skill_tool(self) -> None:
        session = AgentSession(client=FakeClient(), todo_store=TodoStore())
        tool_names = [
            tool["function"]["name"]
            for tool in session.available_tools
            if isinstance(tool.get("function"), dict)
        ]

        self.assertIn("precise-calculate", session.messages[0]["content"])
        self.assertIn("load_skill", session.messages[0]["content"])
        self.assertIn("load_skill", tool_names)
        self.assertEqual(session._tool_source("load_skill"), "skill")
        self.assertEqual(session._tool_source("calculate"), "local")

    def test_executes_load_skill_before_other_tools(self) -> None:
        tool_call = SimpleNamespace(
            id="call_skill",
            function=SimpleNamespace(
                name="load_skill",
                arguments='{"name":"precise-calculate"}',
            ),
        )
        client = FakeClient(
            messages=[
                SimpleNamespace(content=None, tool_calls=[tool_call]),
                SimpleNamespace(content="将按说明书调用计算器。", tool_calls=None),
            ]
        )
        session = AgentSession(client=client, todo_store=TodoStore())

        answer = session.ask("请精确计算 1+1")
        payload = json.loads(client.chat.completions.requests[1]["messages"][-1]["content"])

        self.assertEqual(answer, "将按说明书调用计算器。")
        self.assertEqual(payload["name"], "precise-calculate")
        self.assertIn("calculate", payload["instructions"])
        self.assertEqual(session.trace_steps[1]["detail"], "Agent Skill")

    def test_routes_weather_tool_call_through_mcp_client(self) -> None:
        tool_call = SimpleNamespace(
            id="call_weather",
            function=SimpleNamespace(
                name="get_weather",
                arguments='{"location":"上海","days":1}',
            ),
        )
        client = FakeClient(
            messages=[
                SimpleNamespace(content=None, tool_calls=[tool_call]),
                SimpleNamespace(content="上海今天是晴天。", tool_calls=None),
            ]
        )
        mcp_client = FakeMCPClient()
        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
            mcp_client=mcp_client,
        )

        answer = session.ask("上海今天天气怎么样？")

        self.assertEqual(answer, "上海今天是晴天。")
        self.assertEqual(
            mcp_client.calls,
            [("get_weather", '{"location":"上海","days":1}')],
        )
        self.assertEqual(
            json.loads(
                client.chat.completions.requests[1]["messages"][-1]["content"]
            )["weather"]["location"]["name"],
            "上海",
        )

    def test_automatically_searches_knowledge_before_model(self) -> None:
        client = FakeClient(
            messages=[
                SimpleNamespace(
                    content="RAG 会先检索资料，再生成回答。[agent.md]",
                    tool_calls=None,
                )
            ]
        )
        mcp_client = FakeKnowledgeMCPClient()
        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
            mcp_client=mcp_client,
        )

        answer = session.ask("什么是 RAG？")

        request_messages = client.chat.completions.last_request["messages"]
        self.assertIn("RAG 会先检索资料", request_messages[-1]["content"])
        self.assertIn("agent.md", request_messages[-1]["content"])
        self.assertEqual(json.loads(mcp_client.calls[0][1])["query"], "什么是 RAG？")
        self.assertIn("[agent.md]", answer)
        self.assertEqual(len(session.messages), 3)

    def test_streams_automatic_knowledge_search_activity(self) -> None:
        client = FakeStreamingClient([[stream_chunk("这是知识库回答。")]])
        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
            mcp_client=FakeKnowledgeMCPClient(),
        )

        events = list(
            session.ask_stream("什么是 RAG？", include_tool_activity=True)
        )

        activities = [event for event in events if isinstance(event, ToolActivity)]
        self.assertEqual([event.status for event in activities], ["calling", "completed"])
        self.assertIn("找到 1 个", activities[-1].message)
        self.assertEqual(events[-1], "这是知识库回答。")

    def test_session_sends_previous_conversation_to_model(self) -> None:
        client = FakeClient(
            messages=[
                SimpleNamespace(content="好的，我记住了。", tool_calls=None),
                SimpleNamespace(content="你刚才说你叫小明。", tool_calls=None),
            ]
        )
        session = AgentSession(client=client, todo_store=TodoStore())

        session.ask("我叫小明。")
        answer = session.ask("我刚才说我叫什么？")

        second_request_messages = client.chat.completions.requests[1]["messages"]
        self.assertEqual(answer, "你刚才说你叫小明。")
        self.assertEqual(len(second_request_messages), 4)
        self.assertEqual(second_request_messages[1]["content"], "我叫小明。")
        self.assertEqual(
            second_request_messages[3]["content"], "我刚才说我叫什么？"
        )

    def test_session_restores_conversation_from_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "agent.db"
            conversation_store = SqliteConversationStore(database_path, "session-a")
            first_client = FakeClient(
                messages=[SimpleNamespace(content="好的，我记住了。", tool_calls=None)]
            )
            AgentSession(
                client=first_client,
                todo_store=TodoStore(),
                conversation_store=conversation_store,
            ).ask("我叫小明。")

            second_client = FakeClient(
                messages=[
                    SimpleNamespace(content="你刚才说你叫小明。", tool_calls=None)
                ]
            )
            restored = AgentSession(
                client=second_client,
                todo_store=TodoStore(),
                conversation_store=SqliteConversationStore(
                    database_path, "session-a"
                ),
            )
            answer = restored.ask("我刚才说我叫什么？")

            request_messages = second_client.chat.completions.last_request["messages"]
            self.assertEqual(answer, "你刚才说你叫小明。")
            self.assertEqual(request_messages[1]["content"], "我叫小明。")
            self.assertEqual(request_messages[3]["content"], "我刚才说我叫什么？")

    def test_streams_answer_in_multiple_chunks(self) -> None:
        client = FakeStreamingClient(
            [[stream_chunk("你好，"), stream_chunk("小明！")]]
        )
        session = AgentSession(client=client, todo_store=TodoStore())

        chunks = list(session.ask_stream("你好"))

        self.assertEqual(chunks, ["你好，", "小明！"])
        self.assertEqual(session.messages[-1]["content"], "你好，小明！")
        self.assertTrue(client.chat.completions.requests[0]["stream"])

    def test_streaming_agent_reassembles_tool_call_arguments(self) -> None:
        first_tool_part = SimpleNamespace(
            index=0,
            id="call_add",
            function=SimpleNamespace(name="add_", arguments='{"title":"学习'),
        )
        second_tool_part = SimpleNamespace(
            index=0,
            id=None,
            function=SimpleNamespace(name="todo", arguments='流式输出"}'),
        )
        client = FakeStreamingClient(
            [
                [
                    stream_chunk(tool_calls=[first_tool_part]),
                    stream_chunk(tool_calls=[second_tool_part]),
                ],
                [stream_chunk("任务已经添加。")],
            ]
        )
        store = TodoStore()
        session = AgentSession(client=client, todo_store=store)

        answer = "".join(session.ask_stream("添加任务：学习流式输出"))

        self.assertEqual(answer, "任务已经添加。")
        self.assertEqual(store.list_all()[0]["title"], "学习流式输出")
        self.assertEqual(len(client.chat.completions.requests), 2)

    def test_creates_validated_structured_plan(self) -> None:
        client = FakeClient(
            messages=[
                SimpleNamespace(
                    content=(
                        '{"title":"学习计划","summary":"循序渐进",'
                        '"priority":"high","steps":['
                        '{"title":"阅读","description":"学习概念",'
                        '"minutes":20}]}'
                    ),
                    tool_calls=None,
                )
            ]
        )
        session = AgentSession(client=client, todo_store=TodoStore())

        plan = session.create_plan("学习 Structured Output")

        request = client.chat.completions.last_request
        self.assertEqual(plan.priority, "high")
        self.assertEqual(plan.total_minutes, 20)
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["max_tokens"], 1600)

    def test_accepts_fenced_plan_and_chinese_priority(self) -> None:
        client = FakeClient(
            messages=[
                SimpleNamespace(
                    content=(
                        "```json\n"
                        '{"title":"旅行计划","summary":"准备出发",'
                        '"priority":"高优先级","steps":['
                        '{"title":"订车票","description":"选择车次",'
                        '"minutes":20}]}\n```'
                    ),
                    tool_calls=None,
                )
            ]
        )
        session = AgentSession(client=client, todo_store=TodoStore())

        plan = session.create_plan("准备旅行")

        self.assertEqual(plan.priority, "high")
        self.assertEqual(len(client.chat.completions.requests), 1)

    def test_retries_invalid_plan_with_validation_feedback(self) -> None:
        client = FakeClient(
            messages=[
                SimpleNamespace(
                    content='{"title":"缺少必要字段"}',
                    tool_calls=None,
                ),
                SimpleNamespace(
                    content=(
                        '{"title":"修正计划","summary":"已经修正",'
                        '"priority":"medium","steps":['
                        '{"title":"开始","description":"完成练习",'
                        '"minutes":30}]}'
                    ),
                    tool_calls=None,
                ),
            ]
        )
        session = AgentSession(client=client, todo_store=TodoStore())

        plan = session.create_plan("测试自动修正")

        self.assertEqual(plan.title, "修正计划")
        self.assertEqual(len(client.chat.completions.requests), 2)
        repair_prompt = client.chat.completions.last_request["messages"][-1]["content"]
        self.assertIn("校验问题", repair_prompt)

    def test_adds_only_selected_plan_steps_to_todos(self) -> None:
        store = TodoStore()
        session = AgentSession(client=FakeClient(messages=[]), todo_store=store)

        todos = session.add_plan_todos(["阅读工作流", "完成代码练习"])

        self.assertEqual(len(todos), 2)
        self.assertEqual(
            [todo["title"] for todo in store.list_all()],
            ["阅读工作流", "完成代码练习"],
        )

    def test_rejects_empty_plan_step_selection(self) -> None:
        session = AgentSession(client=FakeClient(messages=[]), todo_store=TodoStore())

        with self.assertRaisesRegex(ValueError, "至少选择一个"):
            session.add_plan_todos([])

    def test_rejects_plan_with_invalid_structure(self) -> None:
        client = FakeClient(
            messages=[
                SimpleNamespace(
                    content='{"title":"缺少必要字段"}',
                    tool_calls=None,
                ),
                SimpleNamespace(
                    content='{"title":"仍然缺少必要字段"}',
                    tool_calls=None,
                ),
            ]
        )
        session = AgentSession(client=client, todo_store=TodoStore())

        with self.assertRaisesRegex(RuntimeError, "格式不符合要求"):
            session.create_plan("测试错误结构")

    def test_agent_can_add_and_list_todo(self) -> None:
        add_call = SimpleNamespace(
            id="call_add",
            function=SimpleNamespace(
                name="add_todo",
                arguments='{"title": "学习 Tool Calling"}',
            ),
        )
        list_call = SimpleNamespace(
            id="call_list",
            function=SimpleNamespace(name="list_todos", arguments="{}"),
        )
        client = FakeClient(
            messages=[
                SimpleNamespace(content=None, tool_calls=[add_call]),
                SimpleNamespace(content="任务已经添加。", tool_calls=None),
                SimpleNamespace(content=None, tool_calls=[list_call]),
                SimpleNamespace(
                    content="你的任务是：学习 Tool Calling。", tool_calls=None
                ),
            ]
        )
        session = AgentSession(client=client, todo_store=TodoStore())

        session.ask("帮我添加任务：学习 Tool Calling")
        answer = session.ask("查看我的任务")

        self.assertEqual(answer, "你的任务是：学习 Tool Calling。")
        self.assertEqual(
            session.todo_store.list_all(),
            [{"id": 1, "title": "学习 Tool Calling", "completed": False}],
        )

    def test_agent_can_read_current_grouped_plans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteTodoStore(Path(directory) / "agent.db", "user-a")
            store.create_plan_with_steps(
                "AI Agent 学习计划",
                "系统学习",
                "high",
                [{"title": "学习工具调用", "description": "完成练习", "minutes": 60}],
            )
            tool_call = SimpleNamespace(
                id="call_plans",
                function=SimpleNamespace(name="list_todo_plans", arguments="{}"),
            )
            client = FakeClient(
                messages=[
                    SimpleNamespace(content=None, tool_calls=[tool_call]),
                    SimpleNamespace(
                        content="你有一个 AI Agent 学习计划，目前还有 1 项未完成。",
                        tool_calls=None,
                    ),
                ]
            )
            session = AgentSession(
                client=client,
                todo_store=store,
                auto_search_knowledge=False,
            )

            answer = session.ask("我当前有哪些计划？")

            tool_result = next(
                message["content"]
                for message in session.messages
                if isinstance(message, dict) and message.get("role") == "tool"
            )
            self.assertIn("AI Agent 学习计划", tool_result)
            self.assertIn("还有 1 项未完成", answer)

    def test_agent_can_confirm_a_saved_travel_plan(self) -> None:
        confirm_call = SimpleNamespace(
            id="call_confirm_travel",
            function=SimpleNamespace(
                name="confirm_travel_plan",
                arguments='{"travel_plan_id": 7}',
            ),
        )
        client = FakeClient(
            messages=[
                SimpleNamespace(content=None, tool_calls=[confirm_call]),
                SimpleNamespace(content="旅行计划 #7 已确认。", tool_calls=None),
            ]
        )
        travel_store = Mock()
        travel_store.confirm.return_value = {
            "id": 7,
            "title": "杭州两日游",
            "status": "confirmed",
            "version": 2,
        }
        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
            travel_store=travel_store,
            auto_search_knowledge=False,
        )

        answer = session.ask("确认旅行计划 7")

        self.assertEqual(answer, "旅行计划 #7 已确认。")
        travel_store.confirm.assert_called_once_with(7)

    def test_agent_completes_todo_after_approval(self) -> None:
        complete_call = SimpleNamespace(
            id="call_complete",
            function=SimpleNamespace(
                name="complete_todo",
                arguments='{"id": 1}',
            ),
        )
        client = FakeClient(
            messages=[
                SimpleNamespace(content=None, tool_calls=[complete_call]),
                SimpleNamespace(content="任务已经完成。", tool_calls=None),
            ]
        )
        store = TodoStore()
        store.add("完成 Agent 练习")
        session = AgentSession(
            client=client,
            todo_store=store,
            approval_callback=lambda _name, _arguments: True,
        )

        answer = session.ask("完成任务 1")

        self.assertEqual(answer, "任务已经完成。")
        self.assertTrue(store.list_all()[0]["completed"])

    def test_agent_does_not_complete_todo_after_rejection(self) -> None:
        complete_call = SimpleNamespace(
            id="call_complete",
            function=SimpleNamespace(
                name="complete_todo",
                arguments='{"id": 1}',
            ),
        )
        client = FakeClient(
            messages=[
                SimpleNamespace(content=None, tool_calls=[complete_call]),
                SimpleNamespace(content="操作已取消。", tool_calls=None),
            ]
        )
        store = TodoStore()
        store.add("完成 Agent 练习")
        session = AgentSession(
            client=client,
            todo_store=store,
            approval_callback=lambda _name, _arguments: False,
        )

        answer = session.ask("完成任务 1")

        self.assertEqual(answer, "操作已取消。")
        self.assertFalse(store.list_all()[0]["completed"])


if __name__ == "__main__":
    unittest.main()
