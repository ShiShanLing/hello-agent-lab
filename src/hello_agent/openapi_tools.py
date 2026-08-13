"""把 OpenAPI 文档转成 Agent 可调用的 HTTP 工具。"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx


MAX_SPEC_BYTES = 400_000
MAX_OPERATIONS = 80
MAX_SELECTED_TOOLS = 20
MAX_RESPONSE_BYTES = 20_000
HTTP_TIMEOUT_SECONDS = 12.0
ALLOWED_METHODS = {"get", "post", "put", "patch", "delete"}
_UNSAFE_METHODS = {"delete"}
_WRITE_METHODS = {"post", "put", "patch", "delete"}


def parse_openapi_document(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("请粘贴 OpenAPI JSON 或 YAML。")
    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise ValueError("OpenAPI 文档过大，请精简后再导入。")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(
            "目前只支持 OpenAPI JSON。请把 YAML 转成 JSON 后再导入。"
        ) from error
    if not isinstance(parsed, dict):
        raise ValueError("OpenAPI 文档必须是对象。")
    if not str(parsed.get("openapi") or parsed.get("swagger") or "").strip():
        raise ValueError("这不是有效的 OpenAPI / Swagger 文档。")
    return parsed


def validate_public_http_url(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("请填写 API 根地址。")
    if len(cleaned) > 500:
        raise ValueError("API 根地址过长。")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("API 根地址只支持 http 或 https。")
    if parsed.username or parsed.password:
        raise ValueError("请勿在 URL 中嵌入账号密码，请使用 Token。")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("API 根地址缺少主机名。")
    if host in {"localhost", "metadata.google.internal", "metadata"} or host.endswith(
        ".localhost"
    ):
        raise ValueError("不允许访问内网或本机地址。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
        except socket.gaierror as error:
            raise ValueError(f"无法解析主机名：{host}") from error
        for info in infos:
            _reject_private_ip(ipaddress.ip_address(info[4][0]))
        return cleaned.rstrip("/")
    _reject_private_ip(address)
    return cleaned.rstrip("/")


def _reject_private_ip(address: ipaddress._BaseAddress) -> None:
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or str(address) == "169.254.169.254"
    ):
        raise ValueError("不允许访问内网或保留地址。")


def extract_operations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise ValueError("OpenAPI 文档没有可用的 paths。")
    operations: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            method_name = str(method).lower()
            if method_name not in ALLOWED_METHODS or not isinstance(operation, dict):
                continue
            operation_id = str(operation.get("operationId") or "").strip()
            tool_name = _unique_tool_name(
                operation_id or f"{method_name}_{path}", used_names
            )
            used_names.add(tool_name)
            summary = str(
                operation.get("summary")
                or operation.get("description")
                or f"{method_name.upper()} {path}"
            ).strip()
            parameters = _collect_parameters(item, operation)
            request_body = operation.get("requestBody")
            schema = _tool_parameter_schema(parameters, request_body)
            operations.append(
                {
                    "operation_id": operation_id or tool_name,
                    "tool_name": tool_name,
                    "method": method_name.upper(),
                    "path": str(path),
                    "summary": summary[:300],
                    "unsafe": method_name in _UNSAFE_METHODS,
                    "write": method_name in _WRITE_METHODS,
                    "parameters": schema,
                    "path_params": [
                        item["name"]
                        for item in parameters
                        if item.get("in") == "path"
                    ],
                    "query_params": [
                        item["name"]
                        for item in parameters
                        if item.get("in") == "query"
                    ],
                    "header_params": [
                        item["name"]
                        for item in parameters
                        if item.get("in") == "header"
                    ],
                    "has_body": _has_json_body(request_body),
                }
            )
            if len(operations) >= MAX_OPERATIONS:
                return operations
    if not operations:
        raise ValueError("OpenAPI 文档没有可导入的 HTTP 操作。")
    return operations


def preview_openapi(
    *,
    name: str,
    spec_text: str,
    base_url: str | None = None,
) -> dict[str, Any]:
    spec = parse_openapi_document(spec_text)
    resolved_base = validate_public_http_url(base_url or _default_base_url(spec))
    operations = extract_operations(spec)
    title = str((spec.get("info") or {}).get("title") or name).strip() or name
    return {
        "name": name.strip() or title,
        "title": title[:80],
        "openapi_version": str(spec.get("openapi") or spec.get("swagger") or ""),
        "base_url": resolved_base,
        "operation_count": len(operations),
        "operations": operations,
    }


def select_operations(
    operations: list[dict[str, Any]],
    selected_ids: list[str] | None,
) -> list[dict[str, Any]]:
    if not selected_ids:
        chosen = [item for item in operations if not item.get("unsafe")][:8]
        if not chosen:
            chosen = operations[:5]
        return chosen
    wanted = {item.strip() for item in selected_ids if item.strip()}
    chosen = [
        item
        for item in operations
        if item["operation_id"] in wanted or item["tool_name"] in wanted
    ]
    if not chosen:
        raise ValueError("请至少勾选一个接口。")
    if len(chosen) > MAX_SELECTED_TOOLS:
        raise ValueError(f"每个 OpenAPI 来源最多启用 {MAX_SELECTED_TOOLS} 个工具。")
    return chosen


def build_tool_definitions(
    source_id: str,
    source_name: str,
    operations: list[dict[str, Any]],
) -> list[dict[str, object]]:
    tools: list[dict[str, object]] = []
    for operation in operations:
        description = (
            f"{operation['summary']}。来源：{source_name}。"
            f"{operation['method']} {operation['path']}。"
        )
        if operation.get("unsafe"):
            description += "这是高风险写操作，调用前必须获得用户确认。"
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": str(operation["tool_name"]),
                    "description": description[:500],
                    "parameters": operation["parameters"],
                },
                "openapi_source_id": source_id,
                "openapi_operation_id": operation["operation_id"],
            }
        )
    return tools


def call_openapi_operation(
    *,
    base_url: str,
    operation: dict[str, Any],
    arguments: dict[str, Any],
    auth_token: str | None = None,
    auth_header: str = "Authorization",
) -> str:
    method = str(operation["method"]).upper()
    path = str(operation["path"])
    for name in operation.get("path_params") or []:
        value = arguments.get(name)
        if value in {None, ""}:
            return json.dumps(
                {"error": f"缺少路径参数：{name}"},
                ensure_ascii=False,
            )
        path = re.sub(
            rf"\{{{re.escape(str(name))}\}}",
            quote(str(value), safe=""),
            path,
        )
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    validate_public_http_url(url)
    query = {
        name: arguments[name]
        for name in operation.get("query_params") or []
        if name in arguments and arguments[name] not in {None, ""}
    }
    headers = {
        name: str(arguments[name])
        for name in operation.get("header_params") or []
        if name in arguments and arguments[name] not in {None, ""}
    }
    token = (auth_token or "").strip()
    if token:
        header_name = (auth_header or "Authorization").strip() or "Authorization"
        if header_name.lower() == "authorization" and not token.lower().startswith(
            ("bearer ", "basic ", "token ")
        ):
            headers[header_name] = f"Bearer {token}"
        else:
            headers[header_name] = token
    body = None
    if operation.get("has_body"):
        reserved = set(
            (operation.get("path_params") or [])
            + (operation.get("query_params") or [])
            + (operation.get("header_params") or [])
        )
        payload = arguments.get("body")
        if payload is None:
            payload = {
                key: value
                for key, value in arguments.items()
                if key not in reserved
            }
        if payload not in {None, ""}:
            body = payload
            headers.setdefault("Content-Type", "application/json")
    try:
        with httpx.Client(
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            kwargs: dict[str, Any] = {
                "headers": headers or None,
                "params": query or None,
            }
            if body is not None:
                kwargs["json"] = body
            response = client.request(method, url, **kwargs)
    except httpx.HTTPError as error:
        return json.dumps(
            {"error": f"调用 OpenAPI 接口失败：{error}"},
            ensure_ascii=False,
        )
    text = response.text[:MAX_RESPONSE_BYTES]
    payload: dict[str, Any] = {
        "status_code": response.status_code,
        "url": url,
        "method": method,
        "text": text,
    }
    if text[:1] in {"{", "["}:
        try:
            payload["json"] = json.loads(text)
        except json.JSONDecodeError:
            pass
    if response.status_code >= 400:
        payload["error"] = f"HTTP {response.status_code}"
    return json.dumps(payload, ensure_ascii=False)


def _default_base_url(spec: dict[str, Any]) -> str:
    servers = spec.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict) and first.get("url"):
            return str(first["url"])
    host = str(spec.get("host") or "").strip()
    schemes = spec.get("schemes")
    scheme = "https"
    if isinstance(schemes, list) and schemes:
        scheme = str(schemes[0])
    base_path = str(spec.get("basePath") or "")
    if host:
        return f"{scheme}://{host}{base_path}"
    raise ValueError("请填写 API 根地址，文档中没有 servers.url。")


def _collect_parameters(
    path_item: dict[str, Any], operation: dict[str, Any]
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in (path_item.get("parameters"), operation.get("parameters")):
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            location = str(item.get("in") or "").strip()
            if not name or location not in {"path", "query", "header"}:
                continue
            key = (name, location)
            if key in seen:
                continue
            seen.add(key)
            collected.append(
                {
                    "name": name,
                    "in": location,
                    "required": bool(item.get("required") or location == "path"),
                    "description": str(item.get("description") or "")[:200],
                    "schema": item.get("schema")
                    if isinstance(item.get("schema"), dict)
                    else {"type": item.get("type") or "string"},
                }
            )
    return collected


def _has_json_body(request_body: object) -> bool:
    if not isinstance(request_body, dict):
        return False
    content = request_body.get("content")
    if not isinstance(content, dict):
        return bool(request_body)
    return any("json" in str(key).lower() for key in content)


def _tool_parameter_schema(
    parameters: list[dict[str, Any]],
    request_body: object,
) -> dict[str, object]:
    properties: dict[str, object] = {}
    required: list[str] = []
    for item in parameters:
        name = str(item["name"])
        schema = dict(item.get("schema") or {"type": "string"})
        if item.get("description"):
            schema["description"] = item["description"]
        properties[name] = schema
        if item.get("required"):
            required.append(name)
    if _has_json_body(request_body):
        body_schema: dict[str, Any] = {"type": "object", "description": "JSON 请求体"}
        if isinstance(request_body, dict):
            content = request_body.get("content")
            if isinstance(content, dict):
                for key, value in content.items():
                    if "json" in str(key).lower() and isinstance(value, dict):
                        maybe_schema = value.get("schema")
                        if isinstance(maybe_schema, dict):
                            body_schema = maybe_schema
                        break
        properties["body"] = body_schema
        if isinstance(request_body, dict) and request_body.get("required"):
            required.append("body")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _unique_tool_name(raw: str, used: set[str]) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", raw).strip("_").lower()
    if not cleaned:
        cleaned = "openapi_tool"
    if cleaned[0].isdigit():
        cleaned = f"op_{cleaned}"
    cleaned = cleaned[:56]
    candidate = cleaned
    index = 2
    while candidate in used:
        suffix = f"_{index}"
        candidate = f"{cleaned[: 56 - len(suffix)]}{suffix}"
        index += 1
    return candidate
