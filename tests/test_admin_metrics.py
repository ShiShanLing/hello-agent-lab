"""管理后台全站费用汇总测试。"""

import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from hello_agent.admin_auth import SqliteAdminAuthService, _totp
from hello_agent.api import create_api
from hello_agent.auth import SqliteAuthService
from hello_agent.database import SqliteObservabilityStore, dispose_database_connections


class AdminMetricsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "agent.db"
        self.client = TestClient(
            create_api(auth_service=SqliteAuthService(self.database_path))
        )
        admin_auth = SqliteAdminAuthService(self.database_path)
        admin_auth.bootstrap(
            "cost-admin@example.com",
            "temporary-admin-password-123",
            "费用管理员",
        )
        password_step = self.client.post(
            "/admin-auth/login",
            json={
                "email": "cost-admin@example.com",
                "password": "temporary-admin-password-123",
            },
        ).json()
        setup = self.client.post(
            "/admin-auth/mfa/setup",
            json={"challenge_token": password_step["challenge_token"]},
        ).json()
        activated = self.client.post(
            "/admin-auth/mfa/activate",
            json={
                "challenge_token": password_step["challenge_token"],
                "code": _totp(setup["secret"], int(time.time() // 30)),
                "new_password": "independent-admin-password-456",
            },
        )
        self.assertEqual(activated.status_code, 200)

    def tearDown(self) -> None:
        dispose_database_connections()
        self.temporary_directory.cleanup()

    def _finish_run(self, user_id: str, prompt_tokens: int) -> None:
        store = SqliteObservabilityStore(self.database_path, user_id)
        run_id = store.start_run("chat", "费用测试", model="deepseek-v4-flash")
        store.finish_run(
            run_id,
            "success",
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 0,
                "total_tokens": prompt_tokens,
            },
            estimated_cost=None,
        )

    def test_admin_metrics_sums_all_users_and_rejects_agent_cookie(self) -> None:
        first = self.client.post(
            "/auth/register",
            json={
                "email": "cost-a@example.com",
                "password": "password123",
                "display_name": "费用甲",
            },
        ).json()
        second_client = TestClient(self.client.app)
        second = second_client.post(
            "/auth/register",
            json={
                "email": "cost-b@example.com",
                "password": "password123",
                "display_name": "费用乙",
            },
        ).json()
        self._finish_run(first["id"], 1_000_000)
        self._finish_run(second["id"], 1_000_000)

        personal = TestClient(self.client.app)
        personal.post(
            "/auth/login",
            json={"email": "cost-a@example.com", "password": "password123"},
        )
        scoped = personal.get("/metrics?days=7")
        self.assertEqual(scoped.status_code, 200)
        self.assertEqual(scoped.json()["runs"]["estimated_cost_usd"], 0.14)
        self.assertEqual(personal.get("/admin/metrics").status_code, 401)

        payload = self.client.get("/admin/metrics?period=day")
        self.assertEqual(payload.status_code, 200)
        body = payload.json()
        self.assertEqual(body["period"], "day")
        self.assertEqual(body["current"]["cost_usd"], 0.28)
        self.assertEqual(body["current"]["tokens"], 2_000_000)
        self.assertEqual(body["current"]["active_users"], 2)
        self.assertEqual(body["current"]["runs"], 2)
        self.assertIn("聊天内容", body["privacy_notice"])
        self.assertEqual(len(body["by_user"]), 2)
        self.assertEqual(sum(item["cost_usd"] for item in body["by_user"]), 0.28)
        names = {item["display_name"] for item in body["by_user"]}
        self.assertEqual(names, {"费用甲", "费用乙"})
        self.assertNotIn("费用测试", str(body))

        weekly = self.client.get("/admin/metrics?period=week").json()
        self.assertEqual(weekly["period"], "week")
        self.assertEqual(weekly["current"]["cost_usd"], 0.28)
        monthly = self.client.get("/admin/metrics?period=month").json()
        self.assertEqual(monthly["period"], "month")
        self.assertEqual(monthly["current"]["cost_usd"], 0.28)
        self.assertEqual(monthly["lookback"]["cost_usd"], 0.28)

        releases = self.client.get("/admin/releases")
        self.assertEqual(releases.status_code, 200)
        self.assertEqual(releases.json()["releases"], [])
        self.assertEqual(personal.get("/admin/releases").status_code, 401)


if __name__ == "__main__":
    unittest.main()
