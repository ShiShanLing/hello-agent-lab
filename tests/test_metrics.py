"""监控看板汇总测试。"""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from hello_agent.api import create_api
from hello_agent.auth import SqliteAuthService
from hello_agent.database import (
    SqliteObservabilityStore,
    SqliteTaskStore,
    dispose_database_connections,
)


class MetricsApiTest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    def test_metrics_aggregates_runs_tokens_and_estimated_usd_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "agent.db"
            client = TestClient(
                create_api(auth_service=SqliteAuthService(database_path))
            )
            user = client.post(
                "/auth/register",
                json={
                    "email": "metrics@example.com",
                    "password": "metrics-pass-123",
                    "display_name": "监控用户",
                },
            ).json()
            store = SqliteObservabilityStore(database_path, user["id"])
            run_id = store.start_run("chat", "费用测试", model="deepseek-v4-flash")
            store.add_step(run_id, "tool", "search_knowledge", "success", 12, "MCP")
            store.finish_run(
                run_id,
                "success",
                usage={
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 0,
                    "total_tokens": 1_000_000,
                },
                estimated_cost=None,
            )
            SqliteTaskStore(database_path, user["id"]).create(
                "workflow_run", "排队中的工作流", {"run_id": "r1"}
            )

            metrics = client.get("/metrics?days=7")
            self.assertEqual(metrics.status_code, 200)
            payload = metrics.json()
            self.assertEqual(payload["pricing"]["currency"], "USD")
            self.assertIn("API Key", payload["pricing"]["note"])
            self.assertEqual(payload["runs"]["total"], 1)
            self.assertEqual(payload["runs"]["success_rate"], 100.0)
            self.assertEqual(payload["runs"]["estimated_cost_usd"], 0.14)
            self.assertEqual(payload["rag"]["calls"], 1)
            self.assertEqual(payload["rag"]["hit_rate"], 100.0)
            self.assertEqual(payload["queue"]["queued"], 1)
            self.assertGreaterEqual(payload["totals"]["estimated_cost_usd"], 0.14)


if __name__ == "__main__":
    unittest.main()
