"""长期记忆存储、抽取、注入和 API 隔离测试。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from hello_agent.api import create_api
from hello_agent.app import AgentSession
from hello_agent.auth import SqliteAuthService
from hello_agent.database import SqliteMemoryStore, dispose_database_connections
from hello_agent.memory import (
    extract_candidate_memories,
    format_memory_context,
    run_memory_tool,
    select_relevant_memories,
)
from hello_agent.tools import TodoStore
from tests.test_app import FakeClient


class MemoryExtractionTest(unittest.TestCase):
    def test_extracts_preferences_facts_and_forget_requests(self) -> None:
        remembered = extract_candidate_memories("请记住我喜欢早起跑步")
        named = extract_candidate_memories("我叫李雷，我是后端工程师。")
        forgotten = extract_candidate_memories("请忘记咖啡")

        self.assertEqual(remembered[0]["action"], "remember")
        self.assertIn("早起跑步", remembered[0]["content"])
        self.assertTrue(any(item["content"] == "李雷" or "李雷" in item["content"] for item in named))
        self.assertEqual(forgotten[0]["action"], "forget")
        self.assertIn("咖啡", forgotten[0]["content"])

    def test_skips_questions_that_are_not_stable_facts(self) -> None:
        self.assertEqual(extract_candidate_memories("我是不是该买咖啡？"), [])
        self.assertEqual(extract_candidate_memories("上海天气怎么样"), [])


class SqliteMemoryStoreTest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    def test_upserts_dedupes_and_isolates_users(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "agent.db"
            first = SqliteMemoryStore(database_path, "user-a")
            second = SqliteMemoryStore(database_path, "user-b")

            saved = first.upsert("preference", "喜欢早起跑步")
            updated = first.upsert("preference", "喜欢早起跑步。")
            second.upsert("constraint", "不要推荐咖啡")

            self.assertEqual(saved["id"], updated["id"])
            self.assertEqual([item["content"] for item in first.list()], ["喜欢早起跑步。"])
            self.assertEqual([item["content"] for item in second.list()], ["不要推荐咖啡"])
            self.assertEqual(first.delete_matching("早起")[0]["id"], updated["id"])
            self.assertEqual(first.list(), [])
            self.assertEqual(len(second.list()), 1)

    def test_enforces_per_user_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteMemoryStore(Path(directory) / "agent.db", "user-a")
            with patch("hello_agent.database.MAX_MEMORIES_PER_USER", 3):
                store.upsert("fact", "第一条记忆内容")
                store.upsert("fact", "第二条记忆内容")
                store.upsert("fact", "第三条记忆内容")
                store.upsert("fact", "第四条记忆内容")

            contents = [item["content"] for item in store.list()]
            self.assertEqual(len(contents), 3)
            self.assertIn("第四条记忆内容", contents)
            self.assertNotIn("第一条记忆内容", contents)


class MemoryContextTest(unittest.TestCase):
    def test_selects_constraints_and_relevant_preferences(self) -> None:
        memories = [
            {"id": "1", "category": "preference", "content": "喜欢早起跑步"},
            {"id": "2", "category": "constraint", "content": "不要推荐咖啡"},
            {"id": "3", "category": "fact", "content": "住在杭州"},
        ]
        selected = select_relevant_memories(memories, "明天早上怎么安排？")
        context = format_memory_context(selected)

        self.assertIsNotNone(context)
        self.assertIn("喜欢早起跑步", context or "")
        self.assertIn("不要推荐咖啡", context or "")

    def test_memory_tools_save_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteMemoryStore(Path(directory) / "agent.db", "user-a")
            saved = json.loads(
                run_memory_tool(
                    "remember_fact",
                    json.dumps(
                        {"category": "preference", "content": "喜欢无糖茶"},
                        ensure_ascii=False,
                    ),
                    store,
                )
            )
            listed = json.loads(run_memory_tool("list_memories", "{}", store))

            self.assertEqual(saved["status"], "saved")
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["memories"][0]["content"], "喜欢无糖茶")
        dispose_database_connections()


class AgentMemoryTest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    def test_injects_relevant_memory_into_model_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteMemoryStore(Path(directory) / "agent.db", "user-a")
            store.upsert("preference", "喜欢早起跑步")
            store.upsert("constraint", "不要推荐咖啡")
            client = FakeClient()
            session = AgentSession(
                client=client,
                todo_store=TodoStore(),
                memory_store=store,
                auto_search_knowledge=False,
            )

            session.ask("明天早上怎么安排？")

            messages = client.chat.completions.last_request["messages"]
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual(messages[1]["role"], "system")
            self.assertIn("喜欢早起跑步", messages[1]["content"])
            self.assertIn("不要推荐咖啡", messages[1]["content"])
            self.assertIn("remember_fact", messages[0]["content"])

    def test_extracts_memory_after_a_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteMemoryStore(Path(directory) / "agent.db", "user-a")
            session = AgentSession(
                client=FakeClient(),
                todo_store=TodoStore(),
                memory_store=store,
                auto_search_knowledge=False,
            )

            session.ask("请记住我喜欢早起")

            self.assertTrue(
                any("早起" in item["content"] for item in store.list())
            )

    def test_remember_fact_tool_writes_to_store(self) -> None:
        tool_call = SimpleNamespace(
            id="call_memory",
            function=SimpleNamespace(
                name="remember_fact",
                arguments='{"category":"constraint","content":"不要推荐咖啡"}',
            ),
        )
        client = FakeClient(
            messages=[
                SimpleNamespace(content=None, tool_calls=[tool_call]),
                SimpleNamespace(content="已经记住了。", tool_calls=None),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteMemoryStore(Path(directory) / "agent.db", "user-a")
            session = AgentSession(
                client=client,
                todo_store=TodoStore(),
                memory_store=store,
                auto_search_knowledge=False,
            )

            answer = session.ask("以后不要给我推荐咖啡")

            self.assertEqual(answer, "已经记住了。")
            self.assertTrue(any("不要推荐咖啡" in item["content"] for item in store.list()))
            self.assertEqual(session._tool_source("remember_fact"), "local")


class MemoryAPITest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "agent.db"
        self.app = create_api(auth_service=SqliteAuthService(self.database_path))
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        dispose_database_connections()
        self.temporary_directory.cleanup()

    def _register(self, email: str, name: str) -> TestClient:
        client = TestClient(self.app)
        response = client.post(
            "/auth/register",
            json={"email": email, "password": "password123", "display_name": name},
        )
        self.assertEqual(response.status_code, 201)
        return client

    def test_requires_login_and_isolates_users(self) -> None:
        anonymous = self.client.get("/memories")
        self.assertEqual(anonymous.status_code, 401)

        first = self._register("memory-a@example.com", "记忆甲")
        second = self._register("memory-b@example.com", "记忆乙")
        created = first.post(
            "/memories",
            json={"category": "preference", "content": "喜欢早起跑步"},
        )
        second.post(
            "/memories",
            json={"category": "constraint", "content": "不要推荐咖啡"},
        )

        self.assertEqual(created.status_code, 201)
        listed = first.get("/memories")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total"], 1)
        self.assertEqual(listed.json()["memories"][0]["content"], "喜欢早起跑步")

        other = second.get("/memories")
        self.assertEqual(other.json()["memories"][0]["content"], "不要推荐咖啡")

        deleted = first.delete(f"/memories/{created.json()['id']}")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(first.get("/memories").json()["total"], 0)
        self.assertEqual(second.get("/memories").json()["total"], 1)

        cleared = second.delete("/memories")
        self.assertEqual(cleared.status_code, 204)
        self.assertEqual(second.get("/memories").json()["total"], 0)
        missing = first.delete("/memories/missing-id")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
