"""发现多个 MCP Server，并适配为 DeepSeek Tool Calling 格式。"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


class MCPClientProtocol(Protocol):
    display_name: str
    transport: str

    @property
    def tools(self) -> list[dict[str, object]]: ...

    @property
    def tool_names(self) -> set[str]: ...

    def call_tool(self, name: str, arguments: str) -> str: ...


def validate_remote_mcp_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("请填写远程 MCP 地址。")
    if len(cleaned) > 500:
        raise ValueError("远程 MCP 地址过长。")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("远程 MCP 只支持 http 或 https。")
    if not parsed.hostname:
        raise ValueError("远程 MCP 地址缺少主机名。")
    if parsed.username or parsed.password:
        raise ValueError("请勿在 URL 中嵌入账号密码，请使用 Token。")
    hostname = parsed.hostname.lower()
    if hostname in {"metadata.google.internal", "metadata"}:
        raise ValueError("不允许连接云元数据地址。")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return cleaned
    if address.is_link_local or str(address) == "169.254.169.254":
        raise ValueError("不允许连接链路本地或云元数据地址。")
    return cleaned


def _tool_definitions_from_session_tools(tools: list[Any]) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "MCP 工具",
                "parameters": tool.inputSchema
                if hasattr(tool, "inputSchema")
                else getattr(tool, "input_schema", {}),
            },
        }
        for tool in tools
    ]


def _format_tool_result(result: Any) -> str:
    if result.is_error:
        message = "\n".join(
            content.text
            for content in result.content
            if getattr(content, "type", None) == "text"
        ) or "MCP Server 返回了未知错误。"
        return json.dumps({"error": message}, ensure_ascii=False)

    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)

    text_parts = [
        content.text
        for content in result.content
        if getattr(content, "type", None) == "text"
    ]
    return json.dumps({"result": "\n".join(text_parts)}, ensure_ascii=False)


class StdioMCPClient:
    """一个本地 STDIO MCP Server 的同步客户端。"""

    def __init__(self, module: str, display_name: str) -> None:
        self.module = module
        self.display_name = display_name
        self.transport = "STDIO"
        self.builtin = True
        self.server_id: str | None = None
        self.url: str | None = None
        self.enabled = True
        self.last_error: str | None = None

    @property
    def server_parameters(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", self.module],
            env=self.extra_environment,
        )

    @property
    def extra_environment(self) -> dict[str, str]:
        return {}

    @property
    def tools(self) -> list[dict[str, object]]:
        return _discover_mcp_tools(self.module)

    @property
    def tool_names(self) -> set[str]:
        return {
            str(tool["function"]["name"])
            for tool in self.tools
            if isinstance(tool.get("function"), dict)
        }

    def call_tool(self, name: str, arguments: str) -> str:
        try:
            parsed_arguments = json.loads(arguments)
            if not isinstance(parsed_arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象。")
        except (json.JSONDecodeError, ValueError) as error:
            return json.dumps({"error": str(error)}, ensure_ascii=False)

        try:
            return anyio.run(self._call_tool, name, parsed_arguments)
        except Exception as error:
            return json.dumps(
                {"error": f"MCP 工具调用失败：{error}"},
                ensure_ascii=False,
            )

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        async with stdio_client(self.server_parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
        return _format_tool_result(result)


class WeatherMCPClient(StdioMCPClient):
    def __init__(self) -> None:
        super().__init__(
            "hello_agent.weather_mcp_server",
            "Hello Agent 天气服务",
        )


class KnowledgeMCPClient(StdioMCPClient):
    def __init__(
        self,
        knowledge_dir: str | None = None,
        knowledge_dirs: list[str] | None = None,
        public_knowledge_dir: str | None = None,
        owner_ids: list[str] | None = None,
        database_path: str | None = None,
    ) -> None:
        super().__init__(
            "hello_agent.knowledge_mcp_server",
            "Hello Agent 本地知识库",
        )
        self.knowledge_dir = knowledge_dir
        self.knowledge_dirs = knowledge_dirs or []
        self.public_knowledge_dir = public_knowledge_dir
        self.owner_ids = owner_ids or []
        self.database_path = database_path

    @property
    def extra_environment(self) -> dict[str, str]:
        environment: dict[str, str] = {}
        if self.knowledge_dirs:
            environment["KNOWLEDGE_DIRS"] = os.pathsep.join(self.knowledge_dirs)
        knowledge_dir = self.knowledge_dir or os.getenv("KNOWLEDGE_DIR")
        if knowledge_dir and "KNOWLEDGE_DIRS" not in environment:
            environment["KNOWLEDGE_DIR"] = knowledge_dir
        public_dir = self.public_knowledge_dir or os.getenv("PUBLIC_KNOWLEDGE_DIR")
        if public_dir:
            environment["PUBLIC_KNOWLEDGE_DIR"] = public_dir
        if self.owner_ids:
            environment["KNOWLEDGE_OWNER_IDS"] = os.pathsep.join(self.owner_ids)
        database_path = self.database_path or os.getenv("TODO_DATABASE_FILE")
        if database_path:
            environment["TODO_DATABASE_FILE"] = database_path
        for name in ("KNOWLEDGE_EMBEDDING_MODEL", "KNOWLEDGE_EMBEDDING_CACHE_DIR"):
            if os.getenv(name):
                environment[name] = os.environ[name]
        return environment


class RemoteMCPClient:
    """连接远程 SSE / Streamable HTTP MCP Server。"""

    def __init__(
        self,
        *,
        name: str,
        url: str,
        transport: str,
        auth_token: str | None = None,
        server_id: str | None = None,
        enabled: bool = True,
    ) -> None:
        self.display_name = name
        self.url = validate_remote_mcp_url(url)
        cleaned_transport = transport.strip().lower()
        if cleaned_transport not in {"sse", "streamable_http"}:
            raise ValueError("传输方式必须是 sse 或 streamable_http。")
        self.transport = cleaned_transport
        self.auth_token = (auth_token or "").strip() or None
        self.server_id = server_id
        self.builtin = False
        self.enabled = enabled
        self.last_error: str | None = None
        self._tools_cache: list[dict[str, object]] | None = None

    @property
    def headers(self) -> dict[str, str]:
        if not self.auth_token:
            return {}
        return {"Authorization": f"Bearer {self.auth_token}"}

    @property
    def tools(self) -> list[dict[str, object]]:
        if self._tools_cache is None:
            return self.refresh_tools()
        return self._tools_cache

    @property
    def tool_names(self) -> set[str]:
        return {
            str(tool["function"]["name"])
            for tool in self.tools
            if isinstance(tool.get("function"), dict)
        }

    def refresh_tools(self) -> list[dict[str, object]]:
        try:
            self._tools_cache = anyio.run(self._list_tools)
            self.last_error = None
        except Exception as error:
            self.last_error = str(error)[:300]
            self._tools_cache = []
        return self._tools_cache

    def call_tool(self, name: str, arguments: str) -> str:
        try:
            parsed_arguments = json.loads(arguments)
            if not isinstance(parsed_arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象。")
        except (json.JSONDecodeError, ValueError) as error:
            return json.dumps({"error": str(error)}, ensure_ascii=False)

        try:
            return anyio.run(self._call_tool, name, parsed_arguments)
        except Exception as error:
            return json.dumps(
                {"error": f"远程 MCP 工具调用失败：{error}"},
                ensure_ascii=False,
            )

    async def _list_tools(self) -> list[dict[str, object]]:
        async with self._open_session() as session:
            result = await session.list_tools()
        return _tool_definitions_from_session_tools(result.tools)

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        async with self._open_session() as session:
            result = await session.call_tool(name, arguments)
        return _format_tool_result(result)

    def _open_session(self):
        return _remote_session(self.url, self.transport, self.headers)


class _remote_session:
    def __init__(self, url: str, transport: str, headers: dict[str, str]) -> None:
        self.url = url
        self.transport = transport
        self.headers = headers
        self._stack: list[Any] = []

    async def __aenter__(self) -> ClientSession:
        if self.transport == "sse":
            transport_cm = sse_client(self.url, headers=self.headers or None)
            read, write = await transport_cm.__aenter__()
            self._stack.append(transport_cm)
        else:
            http_client = create_mcp_http_client(headers=self.headers or None)
            await http_client.__aenter__()
            self._stack.append(http_client)
            transport_cm = streamable_http_client(self.url, http_client=http_client)
            streams = await transport_cm.__aenter__()
            self._stack.append(transport_cm)
            read, write = streams[0], streams[1]
        session = ClientSession(read, write)
        await session.__aenter__()
        self._stack.append(session)
        await session.initialize()
        return session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        while self._stack:
            context = self._stack.pop()
            await context.__aexit__(exc_type, exc, tb)


class MCPClientRegistry:
    """聚合多个 MCP Server，并把工具调用路由给所属服务。"""

    def __init__(self, clients: list[MCPClientProtocol] | None = None) -> None:
        self.clients = (
            clients
            if clients is not None
            else [WeatherMCPClient(), KnowledgeMCPClient()]
        )

    @property
    def tools(self) -> list[dict[str, object]]:
        return [tool for client in self.clients for tool in client.tools]

    @property
    def tool_names(self) -> set[str]:
        return {name for client in self.clients for name in client.tool_names}

    def call_tool(self, name: str, arguments: str) -> str:
        owners = [client for client in self.clients if name in client.tool_names]
        if not owners:
            return json.dumps({"error": f"未知 MCP 工具：{name}"}, ensure_ascii=False)
        if len(owners) > 1:
            return json.dumps(
                {"error": f"多个 MCP Server 注册了同名工具：{name}"},
                ensure_ascii=False,
            )
        return owners[0].call_tool(name, arguments)

    def server_infos(self) -> list[dict[str, object]]:
        infos: list[dict[str, object]] = []
        for client in self.clients:
            tools: list[dict[str, object]] = []
            error = getattr(client, "last_error", None)
            try:
                discovered = client.tools
                tools = [
                    {
                        "name": str(tool["function"]["name"]),
                        "description": str(tool["function"]["description"]),
                        "parameters": tool["function"]["parameters"],
                    }
                    for tool in discovered
                    if isinstance(tool.get("function"), dict)
                ]
                error = getattr(client, "last_error", None)
            except Exception as exc:
                error = str(exc)[:300]
            transport = str(getattr(client, "transport", "STDIO"))
            display_transport = {
                "sse": "SSE",
                "streamable_http": "HTTP",
                "STDIO": "STDIO",
            }.get(transport, transport.upper())
            infos.append(
                {
                    "id": getattr(client, "server_id", None),
                    "name": client.display_name,
                    "transport": display_transport,
                    "builtin": bool(getattr(client, "builtin", True)),
                    "enabled": bool(getattr(client, "enabled", True)),
                    "url": getattr(client, "url", None),
                    "status": "error" if error else "ready",
                    "error_message": error,
                    "tools": tools,
                }
            )
        return infos


def builtin_mcp_clients(
    *,
    user_id: str | None = None,
    database_path: str | Path | None = None,
) -> list[MCPClientProtocol]:
    clients: list[MCPClientProtocol] = [WeatherMCPClient()]
    if user_id is None:
        clients.append(KnowledgeMCPClient())
        return clients
    from hello_agent.knowledge_documents import (
        PUBLIC_KNOWLEDGE_OWNER,
        public_knowledge_dir,
        user_knowledge_dir,
    )

    public_dir = str(public_knowledge_dir())
    user_dir = str(user_knowledge_dir(user_id))
    clients.append(
        KnowledgeMCPClient(
            knowledge_dir=user_dir,
            knowledge_dirs=[public_dir, user_dir],
            public_knowledge_dir=public_dir,
            owner_ids=[PUBLIC_KNOWLEDGE_OWNER, user_id],
            database_path=str(database_path) if database_path else None,
        )
    )
    return clients


def build_user_mcp_registry(
    database_path: Path,
    user_id: str,
) -> MCPClientRegistry:
    from hello_agent.database import SqliteMcpServerStore

    clients = builtin_mcp_clients(user_id=user_id, database_path=database_path)
    store = SqliteMcpServerStore(database_path, user_id)
    for server in store.list(enabled_only=True):
        token = store.auth_token(str(server["id"]))
        remote = RemoteMCPClient(
            name=str(server["name"]),
            url=str(server["url"]),
            transport=str(server["transport"]),
            auth_token=token,
            server_id=str(server["id"]),
            enabled=True,
        )
        remote.refresh_tools()
        if remote.last_error:
            store.update(
                str(server["id"]),
                last_error=remote.last_error,
            )
        else:
            store.update(str(server["id"]), clear_last_error=True)
        clients.append(remote)
    return MCPClientRegistry(clients)


def probe_remote_mcp(
    *,
    name: str,
    url: str,
    transport: str,
    auth_token: str | None = None,
) -> list[dict[str, object]]:
    client = RemoteMCPClient(
        name=name,
        url=url,
        transport=transport,
        auth_token=auth_token,
    )
    tools = client.refresh_tools()
    if client.last_error:
        raise ValueError(f"无法连接远程 MCP：{client.last_error}")
    return tools


async def _list_mcp_tools(module: str) -> list[dict[str, object]]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", module],
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()

    return _tool_definitions_from_session_tools(result.tools)


@lru_cache(maxsize=None)
def _discover_mcp_tools(module: str) -> list[dict[str, object]]:
    """每个后端进程只发现一次同一 MCP Server 的工具。"""
    return anyio.run(_list_mcp_tools, module)
