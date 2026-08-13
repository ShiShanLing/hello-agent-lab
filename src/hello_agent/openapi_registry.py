"""把用户导入的 OpenAPI 来源适配为 Agent 工具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hello_agent.database import SqliteOpenApiSourceStore
from hello_agent.openapi_tools import (
    build_tool_definitions,
    call_openapi_operation,
    extract_operations,
    select_operations,
)


class OpenApiToolRegistry:
    """按账号加载已启用的 OpenAPI 工具，并路由调用。"""

    def __init__(self, sources: list[dict[str, Any]] | None = None) -> None:
        self.sources = sources or []
        self._tools: list[dict[str, object]] = []
        self._operations: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._rebuild()

    def _rebuild(self) -> None:
        tools: list[dict[str, object]] = []
        operations: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for source in self.sources:
            spec = source.get("spec")
            if not isinstance(spec, dict):
                continue
            try:
                extracted = extract_operations(spec)
                selected = select_operations(
                    extracted, list(source.get("selected_operations") or [])
                )
            except ValueError as error:
                source["last_error"] = str(error)[:300]
                continue
            definitions = build_tool_definitions(
                str(source["id"]), str(source["name"]), selected
            )
            for definition, operation in zip(definitions, selected, strict=False):
                function = definition.get("function")
                if not isinstance(function, dict):
                    continue
                name = str(function["name"])
                if name in operations:
                    function["name"] = f"{name}_{str(source['id'])[:8]}"
                    name = str(function["name"])
                operations[name] = (source, operation)
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": function["description"],
                            "parameters": function["parameters"],
                        },
                    }
                )
            source["tools"] = [
                {
                    "name": str(item["function"]["name"]),
                    "description": str(item["function"]["description"]),
                    "parameters": item["function"]["parameters"],
                    "method": operation["method"],
                    "path": operation["path"],
                    "operation_id": operation["operation_id"],
                    "unsafe": bool(operation.get("unsafe")),
                }
                for item, operation in zip(definitions, selected, strict=False)
                if isinstance(item.get("function"), dict)
            ]
        self._tools = tools
        self._operations = operations

    @property
    def tools(self) -> list[dict[str, object]]:
        return list(self._tools)

    @property
    def tool_names(self) -> set[str]:
        return set(self._operations)

    def unsafe_tool_names(self) -> set[str]:
        names: set[str] = set()
        for name, (_source, operation) in self._operations.items():
            if operation.get("unsafe") or operation.get("write"):
                names.add(name)
        return names

    def call_tool(self, name: str, arguments: str) -> str:
        owner = self._operations.get(name)
        if owner is None:
            return json.dumps({"error": f"未知 OpenAPI 工具：{name}"}, ensure_ascii=False)
        try:
            parsed = json.loads(arguments)
            if not isinstance(parsed, dict):
                raise ValueError("工具参数必须是 JSON 对象。")
        except (json.JSONDecodeError, ValueError) as error:
            return json.dumps({"error": str(error)}, ensure_ascii=False)
        source, operation = owner
        return call_openapi_operation(
            base_url=str(source["base_url"]),
            operation=operation,
            arguments=parsed,
            auth_token=source.get("auth_token"),
            auth_header=str(source.get("auth_header") or "Authorization"),
        )

    def source_infos(self) -> list[dict[str, object]]:
        infos: list[dict[str, object]] = []
        for source in self.sources:
            error = source.get("last_error")
            infos.append(
                {
                    "id": source.get("id"),
                    "name": source.get("name"),
                    "base_url": source.get("base_url"),
                    "enabled": bool(source.get("enabled", True)),
                    "has_auth": bool(source.get("has_auth") or source.get("auth_token")),
                    "auth_header": source.get("auth_header") or "Authorization",
                    "selected_operations": source.get("selected_operations") or [],
                    "status": "error" if error else ("disabled" if not source.get("enabled", True) else "ready"),
                    "error_message": error,
                    "tools": source.get("tools") or [],
                }
            )
        return infos


def build_user_openapi_registry(
    database_path: Path, user_id: str
) -> OpenApiToolRegistry:
    store = SqliteOpenApiSourceStore(database_path, user_id)
    sources: list[dict[str, Any]] = []
    for item in store.list(enabled_only=True):
        detail = store.get(str(item["id"]))
        detail["auth_token"] = store.auth_token(str(item["id"]))
        sources.append(detail)
    return OpenApiToolRegistry(sources)
