"""协作 HITL：规划确认门闩的 API。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from hello_agent.api import SessionManager, create_api
from hello_agent.app import AgentSession
from hello_agent.auth import SqliteAuthService
from hello_agent.database import (
    SqliteCollaborationStore,
    SqliteConversationStore,
    dispose_database_connections,
)
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


def _read_sse(response) -> list[dict]:
    events: list[dict] = []
    for block in response.text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data:"):
            continue
        import json

        events.append(json.loads(line.removeprefix("data:").strip()))
    return events


class CollaborationHitlApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "agent.db"
        self.auth = SqliteAuthService(self.database_path)
        self.fake_client = FakeClient(
            messages=[
                _text("1. 用计算器\n2. 给出答案"),
                _tool("call_calc", "calculate", '{"expression":"1+1"}'),
                _text("结果是 2。"),
                _tool(
                    "call_approve",
                    "approve_delivery",
                    '{"answer":"1 加 1 等于 2。"}',
                ),
            ]
        )

        def factory(session_id: str, user_id: str | None = None) -> AgentSession:
            return AgentSession(
                client=self.fake_client,
                todo_store=TodoStore(),
                conversation_store=SqliteConversationStore(
                    self.database_path, session_id
                ),
                auto_search_knowledge=False,
            )

        self.app = create_api(
            session_manager=SessionManager(
                session_factory=factory,
                history_loader=lambda _sid: [],
            ),
            auth_service=self.auth,
        )

    def tearDown(self) -> None:
        dispose_database_connections()
        self.temporary_directory.cleanup()

    def _register(self) -> TestClient:
        client = TestClient(self.app)
        response = client.post(
            "/auth/register",
            json={
                "email": f"hitl-{uuid4().hex[:8]}@example.com",
                "password": "password123",
                "display_name": "HITL用户",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return client

    def test_stream_pauses_then_approve_completes(self) -> None:
        client = self._register()
        stream = client.post(
            "/collaborations/stream",
            json={"goal": "请精确计算 1+1"},
        )
        self.assertEqual(stream.status_code, 200, stream.text)
        events = _read_sse(stream)
        waiting = [
            event
            for event in events
            if event.get("type") == "collaboration"
            and event.get("status") == "waiting_approval"
        ]
        self.assertTrue(waiting)
        collaboration_id = waiting[-1]["collaboration_id"]
        done = [event for event in events if event.get("type") == "done"][-1]
        self.assertTrue(done.get("approval_required"))
        self.assertEqual(done.get("collaboration_id"), collaboration_id)

        approve = client.post(f"/collaborations/{collaboration_id}/approve")
        self.assertEqual(approve.status_code, 200, approve.text)
        approve_events = _read_sse(approve)
        self.assertTrue(
            any(
                event.get("agent") == "executor"
                and event.get("status") == "tool_completed"
                for event in approve_events
            )
        )
        final = [event for event in approve_events if event.get("type") == "done"][-1]
        self.assertEqual(final.get("answer"), "1 加 1 等于 2。")

        me = client.get("/auth/me").json()
        store = SqliteCollaborationStore(self.database_path, me["id"])
        detail = store.get(collaboration_id)
        self.assertEqual(detail["status"], "completed")

    def test_reject_cancels_without_executor(self) -> None:
        client = self._register()
        stream = client.post(
            "/collaborations/stream",
            json={"goal": "请精确计算 1+1"},
        )
        events = _read_sse(stream)
        collaboration_id = [
            event["collaboration_id"]
            for event in events
            if event.get("status") == "waiting_approval"
        ][-1]
        rejected = client.post(f"/collaborations/{collaboration_id}/reject")
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["status"], "cancelled")
        me = client.get("/auth/me").json()
        store = SqliteCollaborationStore(self.database_path, me["id"])
        self.assertEqual(store.get(collaboration_id)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
