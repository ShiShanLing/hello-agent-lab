"""普通对话 vs 多 Agent 协作的自动路由。"""

import unittest

from fastapi.testclient import TestClient

from hello_agent.api import create_api
from hello_agent.auth import SqliteAuthService
from hello_agent.collaboration_routing import route_conversation_mode
from hello_agent.database import dispose_database_connections
import tempfile
from pathlib import Path


class CollaborationRoutingUnitTest(unittest.TestCase):
    def test_simple_calc_and_todo_stays_chat(self) -> None:
        result = route_conversation_mode(
            "请精确计算 (135.5+64.5)*17/4，并把结果记成一条 Todo"
        )
        self.assertFalse(result["use_collaboration"])
        self.assertEqual(result["mode"], "chat")

    def test_weather_and_greeting_stay_chat(self) -> None:
        self.assertFalse(route_conversation_mode("北京今天天气怎么样")["use_collaboration"])
        self.assertFalse(route_conversation_mode("你好")["use_collaboration"])

    def test_explicit_multi_agent_goes_collaboration(self) -> None:
        result = route_conversation_mode("请用多智能体帮我完成这个目标：上线检查清单")
        self.assertTrue(result["use_collaboration"])

    def test_complex_delivery_goes_collaboration(self) -> None:
        result = route_conversation_mode(
            "帮我设计一套从需求梳理、方案评估到落地执行的完整流程，"
            "先分析现状，再制定分阶段计划，最后给出验收标准，并说明风险。"
        )
        self.assertTrue(result["use_collaboration"])
        self.assertGreaterEqual(int(result["score"]), 50)


class CollaborationRoutingAPITest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "agent.db"
        self.app = create_api(auth_service=SqliteAuthService(self.database_path))

    def tearDown(self) -> None:
        dispose_database_connections()
        self.temporary_directory.cleanup()

    def _register(self) -> TestClient:
        client = TestClient(self.app)
        response = client.post(
            "/auth/register",
            json={
                "email": "route-user@example.com",
                "password": "password123",
                "display_name": "路由用户",
            },
        )
        self.assertEqual(response.status_code, 201)
        return client

    def test_routing_endpoint(self) -> None:
        client = self._register()
        simple = client.post(
            "/routing/collaboration",
            json={"message": "请精确计算 1+1，并记成 Todo"},
        )
        self.assertEqual(simple.status_code, 200)
        self.assertFalse(simple.json()["use_collaboration"])

        complex_goal = client.post(
            "/routing/collaboration",
            json={
                "message": (
                    "请先规划再执行：梳理发布检查项，分阶段给出实施方案，"
                    "并附带验收与风险评估。"
                )
            },
        )
        self.assertEqual(complex_goal.status_code, 200)
        self.assertTrue(complex_goal.json()["use_collaboration"])


if __name__ == "__main__":
    unittest.main()
