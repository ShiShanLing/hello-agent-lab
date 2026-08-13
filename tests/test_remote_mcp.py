"""远程 MCP 配置、URL 校验与注册表合并测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from hello_agent.api import create_api
from hello_agent.auth import SqliteAuthService
from hello_agent.database import SqliteMcpServerStore, dispose_database_connections
from hello_agent.mcp_client import (
    MCPClientRegistry,
    RemoteMCPClient,
    WeatherMCPClient,
    validate_remote_mcp_url,
)


class RemoteMcpValidationTest(unittest.TestCase):
    def test_rejects_unsafe_urls(self) -> None:
        with self.assertRaises(ValueError):
            validate_remote_mcp_url("ftp://example.com/mcp")
        with self.assertRaises(ValueError):
            validate_remote_mcp_url("http://user:pass@example.com/mcp")
        with self.assertRaises(ValueError):
            validate_remote_mcp_url("http://169.254.169.254/latest")
        self.assertEqual(
            validate_remote_mcp_url("https://tools.example.com/mcp"),
            "https://tools.example.com/mcp",
        )


class SqliteMcpServerStoreTest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    def test_stores_and_masks_auth_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteMcpServerStore(Path(directory) / "agent.db", "user-a")
            created = store.create(
                "演示服务",
                "https://tools.example.com/mcp",
                "streamable_http",
                "secret-token",
            )
            self.assertTrue(created["has_auth"])
            self.assertNotIn("auth_token", created)
            self.assertEqual(store.auth_token(str(created["id"])), "secret-token")
            store.update(str(created["id"]), enabled=False)
            self.assertFalse(store.list()[0]["enabled"])
            self.assertEqual(store.list(enabled_only=True), [])


class RemoteRegistryTest(unittest.TestCase):
    def test_registry_includes_remote_tools(self) -> None:
        remote = RemoteMCPClient(
            name="Demo",
            url="https://tools.example.com/mcp",
            transport="streamable_http",
            server_id="remote-1",
        )
        remote._tools_cache = [
            {
                "type": "function",
                "function": {
                    "name": "echo_message",
                    "description": "回显",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        registry = MCPClientRegistry([WeatherMCPClient(), remote])
        self.assertIn("echo_message", registry.tool_names)
        self.assertIn("get_weather", registry.tool_names)
        infos = registry.server_infos()
        self.assertEqual(infos[1]["builtin"], False)
        self.assertEqual(infos[1]["transport"], "HTTP")


class RemoteMcpAPITest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "agent.db"
        self.app = create_api(auth_service=SqliteAuthService(self.database_path))
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        dispose_database_connections()
        self.temporary_directory.cleanup()

    def _register(self) -> TestClient:
        client = TestClient(self.app)
        response = client.post(
            "/auth/register",
            json={
                "email": "mcp-user@example.com",
                "password": "password123",
                "display_name": "MCP用户",
            },
        )
        self.assertEqual(response.status_code, 201)
        return client

    def test_creates_lists_toggles_and_deletes_remote_mcp(self) -> None:
        client = self._register()
        fake_tools = [
            {
                "type": "function",
                "function": {
                    "name": "echo_message",
                    "description": "回显文本",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        def fake_refresh(self):
            self._tools_cache = fake_tools
            self.last_error = None
            return fake_tools

        with patch(
            "hello_agent.api.probe_remote_mcp",
            return_value=fake_tools,
        ), patch.object(RemoteMCPClient, "refresh_tools", fake_refresh):
            created = client.post(
                "/mcp/servers",
                json={
                    "name": "演示服务",
                    "url": "https://tools.example.com/mcp",
                    "transport": "streamable_http",
                    "auth_token": "secret",
                },
            )
            self.assertEqual(created.status_code, 201)
            payload = created.json()
            self.assertEqual(payload["name"], "演示服务")
            self.assertTrue(payload["has_auth"])
            self.assertEqual(payload["tools"][0]["name"], "echo_message")

            listed = client.get("/mcp/tools")
            self.assertEqual(listed.status_code, 200)
            names = [item["name"] for item in listed.json()["servers"]]
            self.assertIn("演示服务", names)
            self.assertTrue(any(item.get("builtin") for item in listed.json()["servers"]))

            toggled = client.patch(
                f"/mcp/servers/{payload['id']}",
                json={"enabled": False},
            )
            self.assertEqual(toggled.status_code, 200)
            self.assertFalse(toggled.json()["enabled"])

            deleted = client.delete(f"/mcp/servers/{payload['id']}")
            self.assertEqual(deleted.status_code, 204)
            remaining = [
                item["name"]
                for item in client.get("/mcp/tools").json()["servers"]
                if not item.get("builtin")
            ]
            self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
