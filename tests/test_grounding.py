"""可信问答：引用 grounding、联网兜底、低可信回归收录。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hello_agent.app import AgentSession, ToolActivity
from hello_agent.database import SqliteAgentOpsStore, dispose_database_connections
from hello_agent.tools import TodoStore, run_tool
from hello_agent.web_search import search_web

from tests.test_app import FakeKnowledgeMCPClient, FakeStreamingClient, stream_chunk


class GroundingFlowTest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    def test_knowledge_hit_sets_citation_sources(self) -> None:
        client = FakeStreamingClient([[stream_chunk("依据资料，RAG 先检索再生成。[agent.md#片段1]")]])
        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
            mcp_client=FakeKnowledgeMCPClient(has_results=True),
        )

        answer = "".join(
            event
            for event in session.ask_stream("什么是 RAG？", include_tool_activity=True)
            if isinstance(event, str)
        )

        self.assertIn("RAG", answer)
        self.assertEqual(session.last_grounding, "knowledge")
        self.assertEqual(session.last_confidence, "high")
        self.assertEqual(session.last_sources[0]["kind"], "knowledge")
        self.assertFalse(session.last_capture_grounding_failure)
        request_messages = client.chat.completions.requests[0]["messages"]
        self.assertTrue(
            any(
                isinstance(item, dict)
                and item.get("role") == "system"
                and "本地知识库" in str(item.get("content", ""))
                for item in request_messages
            )
        )

    def test_knowledge_miss_falls_back_to_web_search(self) -> None:
        client = FakeStreamingClient([[stream_chunk("根据网页摘要，MCP 是工具协议。[示例页](https://example.com)")]])
        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
            mcp_client=FakeKnowledgeMCPClient(has_results=False),
        )
        fake_web = {
            "query": "MCP",
            "results": [
                {
                    "title": "示例页",
                    "url": "https://example.com/mcp",
                    "snippet": "MCP 让模型调用外部工具。",
                    "source": "web",
                }
            ],
            "provider": "test",
            "message": None,
        }

        with patch("hello_agent.app.search_web", return_value=fake_web), patch(
            "hello_agent.app.web_search_enabled", return_value=True
        ):
            events = list(
                session.ask_stream("什么是 MCP？", include_tool_activity=True)
            )

        activities = [event for event in events if isinstance(event, ToolActivity)]
        self.assertTrue(any(item.tool_name == "web_search" for item in activities))
        self.assertEqual(session.last_grounding, "web")
        self.assertEqual(session.last_sources[0]["kind"], "web")
        self.assertFalse(session.last_capture_grounding_failure)
        request_messages = client.chat.completions.requests[0]["messages"]
        self.assertTrue(
            any(
                isinstance(item, dict)
                and "联网搜索" in str(item.get("content", ""))
                and "禁止说" in str(item.get("content", ""))
                for item in request_messages
            )
        )

    def test_web_results_without_snippet_are_ignored(self) -> None:
        client = FakeStreamingClient([[stream_chunk("未找到依据。")]])
        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
            mcp_client=FakeKnowledgeMCPClient(has_results=False),
        )
        fake_web = {
            "query": "x",
            "results": [
                {
                    "title": "空摘要页",
                    "url": "https://example.com/empty",
                    "snippet": "",
                    "source": "web",
                }
            ],
            "provider": "test",
            "message": None,
        }
        with patch("hello_agent.app.search_web", return_value=fake_web), patch(
            "hello_agent.app.web_search_enabled", return_value=True
        ):
            "".join(
                event
                for event in session.ask_stream("某冷门事实？")
                if isinstance(event, str)
            )
        self.assertEqual(session.last_grounding, "refused")
        self.assertEqual(session.last_sources, [])
        self.assertTrue(session.last_capture_grounding_failure)

    def test_knowledge_and_web_miss_marks_refused_for_capture(self) -> None:
        client = FakeStreamingClient([[stream_chunk("未找到依据。")]])
        session = AgentSession(
            client=client,
            todo_store=TodoStore(),
            mcp_client=FakeKnowledgeMCPClient(has_results=False),
        )

        with patch(
            "hello_agent.app.search_web",
            return_value={"query": "x", "results": [], "provider": "test", "message": "无"},
        ), patch("hello_agent.app.web_search_enabled", return_value=True):
            "".join(
                event
                for event in session.ask_stream("火星市中心今天的市长是谁？")
                if isinstance(event, str)
            )

        self.assertEqual(session.last_grounding, "refused")
        self.assertEqual(session.last_confidence, "low")
        self.assertEqual(session.last_sources, [])
        self.assertTrue(session.last_capture_grounding_failure)
        request_messages = client.chat.completions.requests[0]["messages"]
        self.assertTrue(
            any(
                isinstance(item, dict)
                and "未找到依据" in str(item.get("content", ""))
                for item in request_messages
            )
        )

    def test_capture_grounding_failure_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "agent.db"
            store = SqliteAgentOpsStore(database_path, "user-1")
            first = store.capture_grounding_failure(
                question="火星市中心今天的市长是谁？",
                answer="未找到依据",
                confidence="low",
                grounding="refused",
            )
            second = store.capture_grounding_failure(
                question="火星市中心今天的市长是谁？",
                answer="未找到依据",
                confidence="low",
                grounding="refused",
            )
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            config = store.config()
            grounding_datasets = [
                item for item in config["datasets"] if item["name"] == "可信问答回归集"
            ]
            self.assertEqual(len(grounding_datasets), 1)
            self.assertEqual(len(grounding_datasets[0]["cases"]), 1)

    def test_web_search_tool_runs(self) -> None:
        fake = {
            "query": "hello",
            "results": [
                {
                    "title": "Hello",
                    "url": "https://example.com",
                    "snippet": "world",
                    "source": "web",
                }
            ],
            "provider": "test",
            "message": None,
        }
        with patch("hello_agent.tools.search_web", return_value=fake):
            result = json.loads(
                run_tool("web_search", json.dumps({"query": "hello", "limit": 2}))
            )
        self.assertEqual(result["results"][0]["title"], "Hello")

    def test_search_web_disabled(self) -> None:
        with patch.dict("os.environ", {"WEB_SEARCH_ENABLED": "false"}):
            result = search_web("anything")
        self.assertEqual(result["provider"], "disabled")
        self.assertEqual(result["results"], [])

    def test_bing_cn_html_parser(self) -> None:
        from hello_agent.web_search import _parse_bing_cn_html

        html = """
        <li class="b_algo">
          <a class="tilk" href="https://example.com/mcp">x</a>
          <h2>MCP 介绍</h2>
          <p>这是一段关于 Model Context Protocol 的摘要内容，足够长。</p>
        </li>
        """
        results = _parse_bing_cn_html(html, limit=3)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "MCP 介绍")
        self.assertEqual(results[0]["url"], "https://example.com/mcp")
        self.assertIn("Model Context Protocol", results[0]["snippet"])


if __name__ == "__main__":
    unittest.main()
