"""管理后台行情快照：解析、缓存策略与接口鉴权。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from hello_agent.admin_auth import SqliteAdminAuthService, _totp
from hello_agent.api import create_api
from hello_agent.auth import SqliteAuthService
from hello_agent.database import dispose_database_connections
from hello_agent.market_indices import (
    _classify,
    _direction_label,
    _parse_row,
    should_fetch,
)

_CST = timezone(timedelta(hours=8))
_SAMPLE = {
    "date": "2026-09-03",
    "generated_at": "2026-09-03T15:10:00+08:00",
    "count": 1,
    "pinned": [{"name": "上证指数", "secid": "1.000001", "pct": 0.5}],
    "rest": [],
    "board_count": 0,
    "etf_count": 0,
}


class MarketIndexParseTest(unittest.TestCase):
    def test_parse_row_classifies_index_board_and_etf(self) -> None:
        index = _parse_row({"f2": 3200.12, "f3": 1.25, "f4": 40, "f6": 2e10, "f12": "000001", "f13": 1, "f14": "上证指数", "f62": 0, "f184": 0, "f104": 10, "f105": 8, "f106": 2})
        self.assertEqual(index["type"], "index")
        self.assertEqual(index["name"], "上证指数")
        self.assertEqual(index["secid"], "1.000001")
        self.assertEqual(index["pct"], 1.25)
        self.assertEqual(index["direction"], "涨")
        self.assertEqual(_classify("90.880505"), "board")
        self.assertEqual(_classify("1.512480"), "etf")
        self.assertEqual(_direction_label(-1.2)["direction"], "跌")
        self.assertEqual(_direction_label(0.01)["direction"], "平")


class MarketIndexCachePolicyTest(unittest.TestCase):
    def test_should_fetch_during_weekday_session(self) -> None:
        now = datetime(2026, 9, 3, 10, 0, tzinfo=_CST)
        with patch("hello_agent.market_indices.datetime") as mocked:
            mocked.now.return_value = now
            mocked.fromisoformat = datetime.fromisoformat
            self.assertTrue(should_fetch(_SAMPLE))

    def test_should_not_fetch_when_cache_is_final_after_close(self) -> None:
        now = datetime(2026, 9, 3, 16, 0, tzinfo=_CST)
        with patch("hello_agent.market_indices.datetime") as mocked:
            mocked.now.return_value = now
            mocked.fromisoformat = datetime.fromisoformat
            self.assertFalse(should_fetch(_SAMPLE))

    def test_should_fetch_when_cache_missing(self) -> None:
        now = datetime(2026, 9, 5, 16, 0, tzinfo=_CST)  # Saturday
        with patch("hello_agent.market_indices.datetime") as mocked:
            mocked.now.return_value = now
            mocked.fromisoformat = datetime.fromisoformat
            self.assertTrue(should_fetch(None))


class MarketIndicesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "agent.db"
        self.client = TestClient(create_api(auth_service=SqliteAuthService(self.database_path)))
        admin_auth = SqliteAdminAuthService(self.database_path)
        admin_auth.bootstrap(
            "market-admin@example.com",
            "temporary-admin-password-123",
            "行情管理员",
        )
        password_step = self.client.post(
            "/admin-auth/login",
            json={"email": "market-admin@example.com", "password": "temporary-admin-password-123"},
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

    def test_admin_market_indices_uses_cache_and_rejects_agent_cookie(self) -> None:
        personal = TestClient(self.client.app)
        personal.post(
            "/auth/register",
            json={"email": "market-user@example.com", "password": "password123", "display_name": "普通用户"},
        )
        self.assertEqual(personal.get("/admin/market-indices").status_code, 401)

        with (
            patch("hello_agent.market_indices.load_cache", return_value=_SAMPLE),
            patch("hello_agent.market_indices.fetch_market_indices", new_callable=AsyncMock) as fetch,
        ):
            response = self.client.get("/admin/market-indices")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["date"], "2026-09-03")
        self.assertEqual(body["pinned"][0]["name"], "上证指数")
        fetch.assert_not_called()

        with (
            patch("hello_agent.market_indices.load_cache", return_value=_SAMPLE),
            patch("hello_agent.market_indices.should_fetch", return_value=False),
            patch("hello_agent.market_indices.fetch_market_indices", new_callable=AsyncMock) as fetch,
        ):
            refreshed = self.client.get("/admin/market-indices?refresh=true")
        self.assertEqual(refreshed.status_code, 200)
        self.assertTrue(refreshed.json()["from_cache"])
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
