"""OpenAPI → Agent 工具：解析、安全校验、注册与 API。"""

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from hello_agent.api import create_api
from hello_agent.app import AgentSession
from hello_agent.auth import SqliteAuthService
from hello_agent.database import SqliteOpenApiSourceStore, dispose_database_connections
from hello_agent.openapi_registry import build_user_openapi_registry
from hello_agent.openapi_tools import (
    call_openapi_operation,
    extract_operations,
    parse_openapi_document,
    preview_openapi,
    select_operations,
    validate_public_http_url,
)


SAMPLE_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Pet Store", "version": "1.0.0"},
    "servers": [{"url": "https://example.com"}],
    "paths": {
        "/pets/{id}": {
            "get": {
                "operationId": "getPet",
                "summary": "获取宠物",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            }
        },
        "/pets": {
            "post": {
                "operationId": "createPet",
                "summary": "创建宠物",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                            }
                        }
                    },
                },
            },
            "delete": {
                "operationId": "deletePets",
                "summary": "删除全部宠物",
            },
        },
    },
}

PUBLIC_ADDRINFO = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
]


def _public_dns(*_args, **_kwargs):
    return PUBLIC_ADDRINFO


class OpenApiParseTest(unittest.TestCase):
    @patch("hello_agent.openapi_tools.socket.getaddrinfo", side_effect=_public_dns)
    def test_parses_operations_and_defaults_selection(self, _mock_dns) -> None:
        preview = preview_openapi(
            name="演示",
            spec_text=json.dumps(SAMPLE_SPEC),
        )
        self.assertEqual(preview["base_url"], "https://example.com")
        self.assertEqual(preview["operation_count"], 3)
        names = {item["tool_name"] for item in preview["operations"]}
        self.assertIn("getpet", names)
        self.assertIn("createpet", names)
        self.assertIn("deletepets", names)
        selected = select_operations(list(preview["operations"]), None)
        self.assertTrue(all(not item["unsafe"] for item in selected))
        self.assertLessEqual(len(selected), 8)

    def test_rejects_private_base_url(self) -> None:
        with self.assertRaises(ValueError):
            validate_public_http_url("http://127.0.0.1/api")
        with self.assertRaises(ValueError):
            validate_public_http_url("http://169.254.169.254/latest")
        with self.assertRaises(ValueError):
            validate_public_http_url("http://localhost/api")

    def test_rejects_yaml(self) -> None:
        with self.assertRaises(ValueError):
            parse_openapi_document("openapi: 3.0.0\ninfo:\n  title: Demo\n")


class OpenApiCallTest(unittest.TestCase):
    @patch("hello_agent.openapi_tools.socket.getaddrinfo", side_effect=_public_dns)
    def test_call_builds_request_and_returns_json(self, _mock_dns) -> None:
        operations = extract_operations(SAMPLE_SPEC)
        get_pet = next(item for item in operations if item["operation_id"] == "getPet")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"id":"1","name":"Rex"}'

        with patch("hello_agent.openapi_tools.httpx.Client") as client_cls:
            client = MagicMock()
            client_cls.return_value.__enter__.return_value = client
            client.request.return_value = mock_response
            result = json.loads(
                call_openapi_operation(
                    base_url="https://example.com",
                    operation=get_pet,
                    arguments={"id": "1"},
                    auth_token="secret",
                )
            )
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["json"]["name"], "Rex")
        kwargs = client.request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertTrue(str(client.request.call_args.args[1]).endswith("/pets/1"))


class OpenApiStoreAndRegistryTest(unittest.TestCase):
    def tearDown(self) -> None:
        dispose_database_connections()

    @patch("hello_agent.openapi_tools.socket.getaddrinfo", side_effect=_public_dns)
    def test_store_masks_token_and_registry_exposes_tools(self, _mock_dns) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteOpenApiSourceStore(Path(directory) / "agent.db", "user-a")
            created = store.create(
                "宠物店",
                "https://example.com",
                SAMPLE_SPEC,
                ["getPet", "createPet"],
                "secret-token",
            )
            self.assertTrue(created["has_auth"])
            self.assertNotIn("auth_token", created)
            self.assertEqual(store.auth_token(str(created["id"])), "secret-token")

            registry = build_user_openapi_registry(Path(directory) / "agent.db", "user-a")
            self.assertIn("getpet", registry.tool_names)
            self.assertIn("createpet", registry.tool_names)
            self.assertIn("createpet", registry.unsafe_tool_names())
            self.assertNotIn("getpet", registry.unsafe_tool_names())

            agent = AgentSession(openapi_registry=registry)
            names = {
                str(tool["function"]["name"])
                for tool in agent.available_tools
                if isinstance(tool.get("function"), dict)
            }
            self.assertIn("getpet", names)
            self.assertIn("createpet", agent._confirmation_required_tools())
            self.assertNotIn("getpet", agent._confirmation_required_tools())


class OpenApiAPITest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "agent.db"
        self.app = create_api(auth_service=SqliteAuthService(self.database_path))
        self.client = TestClient(self.app)
        self._dns_patch = patch(
            "hello_agent.openapi_tools.socket.getaddrinfo",
            side_effect=_public_dns,
        )
        self._dns_patch.start()

    def tearDown(self) -> None:
        self._dns_patch.stop()
        dispose_database_connections()
        self.temporary_directory.cleanup()

    def _register(self) -> TestClient:
        client = TestClient(self.app)
        response = client.post(
            "/auth/register",
            json={
                "email": "openapi-user@example.com",
                "password": "password123",
                "display_name": "OpenAPI用户",
            },
        )
        self.assertEqual(response.status_code, 201)
        return client

    def test_preview_create_list_toggle_delete(self) -> None:
        client = self._register()
        preview = client.post(
            "/openapi/preview",
            json={
                "name": "宠物店",
                "spec_text": json.dumps(SAMPLE_SPEC),
            },
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["operation_count"], 3)

        created = client.post(
            "/openapi/sources",
            json={
                "name": "宠物店",
                "spec_text": json.dumps(SAMPLE_SPEC),
                "selected_operations": ["getPet"],
                "auth_token": "token-1",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        self.assertTrue(payload["has_auth"])
        self.assertEqual(len(payload["tools"]), 1)
        self.assertEqual(payload["tools"][0]["name"], "getpet")
        source_id = payload["id"]

        listed = client.get("/openapi/sources")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["sources"]), 1)

        disabled = client.patch(
            f"/openapi/sources/{source_id}",
            json={"enabled": False},
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["enabled"])

        deleted = client.delete(f"/openapi/sources/{source_id}")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(client.get("/openapi/sources").json()["sources"], [])

    def test_rejects_private_url_on_preview(self) -> None:
        client = self._register()
        response = client.post(
            "/openapi/preview",
            json={
                "name": "内网",
                "spec_text": json.dumps(
                    {
                        **SAMPLE_SPEC,
                        "servers": [{"url": "http://127.0.0.1:8000"}],
                    }
                ),
            },
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
