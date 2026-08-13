"""工具结果 Offload：阈值卸载、按需取回与会话隔离。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from hello_agent.app import AgentSession
from hello_agent.database import SqliteToolResultStore, dispose_database_connections
from hello_agent.tool_offload import (
    OFFLOAD_THRESHOLD_CHARS,
    FETCH_TOOL_NAME,
    build_offload_stub,
    offload_tool_result,
    run_fetch_tool_result,
    should_offload,
)


class OffloadPolicyTest(unittest.TestCase):
    def test_threshold_and_skip_fetch(self) -> None:
        small = "x" * (OFFLOAD_THRESHOLD_CHARS - 1)
        large = "y" * OFFLOAD_THRESHOLD_CHARS
        self.assertFalse(should_offload(small, "run_python"))
        self.assertTrue(should_offload(large, "run_python"))
        self.assertFalse(should_offload(large, FETCH_TOOL_NAME))
        already = build_offload_stub(
            ref="abc", tool_name="run_python", content=large
        )
        self.assertFalse(should_offload(already, "run_python"))

    def test_failed_result_keeps_error_flag(self) -> None:
        content = json.dumps({"error": "boom", "detail": "x" * 5000})
        stub = json.loads(
            build_offload_stub(ref="r1", tool_name="mcp_tool", content=content)
        )
        self.assertTrue(stub["offloaded"])
        self.assertEqual(stub["error"], "boom")


class OffloadStoreTest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    def test_put_get_and_prune(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteToolResultStore(Path(directory) / "agent.db", "user-a")
            store.MAX_BLOBS_PER_SESSION = 3
            ids = [
                store.put(
                    session_id="s1",
                    tool_call_id=f"call-{index}",
                    tool_name="run_python",
                    content=f"payload-{index}-" + ("z" * 100),
                )
                for index in range(5)
            ]
            self.assertIsNone(store.get(ids[0]))
            self.assertIsNone(store.get(ids[1]))
            self.assertIsNotNone(store.get(ids[-1]))
            other = SqliteToolResultStore(Path(directory) / "agent.db", "user-b")
            self.assertIsNone(other.get(ids[-1]))


class OffloadIntegrationTest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    def test_agent_offloads_and_fetches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "agent.db"
            store = SqliteToolResultStore(db, "user-a")
            agent = AgentSession(
                tool_result_store=store,
                conversation_store=MagicMock(session_id="sess-1", list_recent=lambda _n: []),
                memory_store=None,
                auto_search_knowledge=False,
            )
            large = json.dumps({"result": "A" * OFFLOAD_THRESHOLD_CHARS}, ensure_ascii=False)
            prepared = agent._prepare_tool_message_content(
                "run_python", "call-1", large
            )
            stub = json.loads(prepared)
            self.assertTrue(stub["offloaded"])
            self.assertLess(len(prepared), len(large))
            fetched = json.loads(
                agent._run_tool(
                    FETCH_TOOL_NAME,
                    json.dumps({"ref": stub["ref"]}),
                )
            )
            self.assertEqual(fetched["result"], "A" * OFFLOAD_THRESHOLD_CHARS)

    def test_local_cache_fallback_without_store(self) -> None:
        cache: dict[str, str] = {}
        large = "k" * OFFLOAD_THRESHOLD_CHARS
        stub = json.loads(
            offload_tool_result(
                content=large,
                tool_name="list_todos",
                tool_call_id="c1",
                session_id="s",
                store=None,
                local_cache=cache,
            )
        )
        self.assertTrue(stub["ref"].startswith("local-"))
        restored = run_fetch_tool_result(
            json.dumps({"ref": stub["ref"]}),
            store=None,
            local_cache=cache,
        )
        self.assertEqual(restored, large)


if __name__ == "__main__":
    unittest.main()
