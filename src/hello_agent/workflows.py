"""可视化工作流的图校验、事件流执行与 Agent 工具编排。"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from collections.abc import Iterator
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, model_validator

from hello_agent.app import create_client
from hello_agent.knowledge import search_knowledge
from hello_agent.mcp_client import MCPClientRegistry
from hello_agent.model_routing import create_chat_completion
from hello_agent.skills import load_skill
from hello_agent.tools import ALL_TOOLS, TodoStoreProtocol, run_tool


NodeType = Literal[
    "input",
    "agent",
    "knowledge",
    "llm",
    "mcp",
    "todo",
    "http",
    "transform",
    "foreach",
    "subworkflow",
    "condition",
    "approval",
    "output",
]
MAX_SUBWORKFLOW_DEPTH = 3
MAX_FOREACH_ITEMS = 20
MAX_HTTP_BODY_BYTES = 200_000
_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
WORKFLOW_LOCAL_TOOLS = {"add_todo", "list_todos", "calculate"}


class WorkflowPosition(BaseModel):
    x: float
    y: float


class WorkflowNode(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    type: NodeType
    label: str = Field(min_length=1, max_length=80)
    position: WorkflowPosition
    config: dict[str, object] = Field(default_factory=dict)


class WorkflowEdge(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    source: str
    target: str
    source_handle: str | None = None


class WorkflowDefinition(BaseModel):
    nodes: list[WorkflowNode] = Field(min_length=2, max_length=50)
    edges: list[WorkflowEdge] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowDefinition":
        node_ids = [node.id for node in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("节点 ID 不能重复。")
        known = set(node_ids)
        if any(edge.source not in known or edge.target not in known for edge in self.edges):
            raise ValueError("连线包含不存在的节点。")
        if not any(node.type == "input" for node in self.nodes):
            raise ValueError("工作流至少需要一个输入节点。")
        if not any(node.type == "output" for node in self.nodes):
            raise ValueError("工作流至少需要一个输出节点。")
        _topological_order(self.nodes, self.edges)
        return self


class WorkflowLookup(Protocol):
    def get(self, workflow_id: str) -> dict[str, object]: ...


class WorkflowRunStep(BaseModel):
    node_id: str
    node_type: NodeType
    label: str
    status: Literal["running", "success", "failed", "skipped", "waiting"]
    duration_ms: int = 0
    summary: str
    input: object | None = None
    output: object | None = None


def stream_workflow(
    definition: WorkflowDefinition,
    input_text: str,
    knowledge_roots: list[Path],
    public_knowledge_root: Path | None = None,
    mcp_client: MCPClientRegistry | None = None,
    approved: bool = False,
    todo_store: TodoStoreProtocol | None = None,
    workflow_store: WorkflowLookup | None = None,
    current_workflow_id: str | None = None,
    depth: int = 0,
    variables: dict[str, object] | None = None,
) -> Iterator[dict[str, object]]:
    """逐节点执行工作流，并在状态变化时立即产生可用于 SSE 的事件。"""
    ordered = _topological_order(definition.nodes, definition.edges)
    node_map = {node.id: node for node in definition.nodes}
    incoming: dict[str, list[WorkflowEdge]] = {node.id: [] for node in definition.nodes}
    outgoing: dict[str, list[WorkflowEdge]] = {node.id: [] for node in definition.nodes}
    for edge in definition.edges:
        incoming[edge.target].append(edge)
        outgoing[edge.source].append(edge)

    values: dict[str, object] = {}
    active_edges: set[str] = set()
    steps: list[dict[str, object]] = []
    registry = mcp_client or MCPClientRegistry()
    run_variables = dict(variables or {})
    yield {"type": "run_started"}

    for node_id in ordered:
        node = node_map[node_id]
        parent_edges = incoming[node_id]
        parents = [values[edge.source] for edge in parent_edges if edge.id in active_edges]
        should_run = node.type == "input" or bool(parents)
        if not should_run:
            step = WorkflowRunStep(
                node_id=node.id, node_type=node.type, label=node.label,
                status="skipped", summary="上游条件未命中，已跳过",
            ).model_dump()
            steps.append(step)
            yield {"type": "node_skipped", "step": step}
            continue

        started = perf_counter()
        mapped_parents = _mapped_parents(node, parents)
        node_input: object = mapped_parents[0] if len(mapped_parents) == 1 else mapped_parents
        yield {
            "type": "node_started",
            "step": WorkflowRunStep(
                node_id=node.id, node_type=node.type, label=node.label,
                status="running", summary="正在执行", input=node_input,
            ).model_dump(),
        }

        if node.type == "approval" and not approved:
            step = WorkflowRunStep(
                node_id=node.id, node_type=node.type, label=node.label,
                status="waiting", summary=str(node.config.get("message", "请确认是否继续执行")),
                input=node_input,
            ).model_dump()
            steps.append(step)
            yield {"type": "approval_required", "step": step, "steps": steps}
            return

        try:
            value, summary, branch = _execute_node(
                node, mapped_parents, input_text, knowledge_roots,
                public_knowledge_root, registry, todo_store,
                workflow_store=workflow_store,
                current_workflow_id=current_workflow_id,
                depth=depth,
                variables=run_variables,
            )
            _save_variable(node, value, run_variables)
            values[node_id] = value
            for edge in outgoing[node_id]:
                if branch is None or edge.source_handle in {None, branch}:
                    active_edges.add(edge.id)
            step = WorkflowRunStep(
                node_id=node.id, node_type=node.type, label=node.label,
                status="success", duration_ms=max(0, int((perf_counter() - started) * 1000)),
                summary=summary, input=node_input, output=value,
            ).model_dump()
            steps.append(step)
            yield {"type": "node_completed", "step": step}
        except Exception as error:
            step = WorkflowRunStep(
                node_id=node.id, node_type=node.type, label=node.label,
                status="failed", duration_ms=max(0, int((perf_counter() - started) * 1000)),
                summary=str(error), input=node_input,
            ).model_dump()
            steps.append(step)
            yield {"type": "node_failed", "step": step, "steps": steps, "message": str(error)}
            return

    output_nodes = [node for node in definition.nodes if node.type == "output"]
    output_values = [values[node.id] for node in output_nodes if node.id in values]
    final_value = output_values[0] if len(output_values) == 1 else output_values
    yield {"type": "run_completed", "output": _final_text(final_value), "steps": steps}


def execute_workflow(
    definition: WorkflowDefinition,
    input_text: str,
    knowledge_roots: list[Path],
    public_knowledge_root: Path | None = None,
    mcp_client: MCPClientRegistry | None = None,
    todo_store: TodoStoreProtocol | None = None,
    workflow_store: WorkflowLookup | None = None,
    current_workflow_id: str | None = None,
    depth: int = 0,
    variables: dict[str, object] | None = None,
) -> tuple[str, list[dict[str, object]]]:
    """兼容普通 HTTP 接口：自动通过审批并收集事件流最终结果。"""
    steps: list[dict[str, object]] = []
    output = ""
    for event in stream_workflow(
        definition, input_text, knowledge_roots, public_knowledge_root,
        mcp_client, approved=True, todo_store=todo_store,
        workflow_store=workflow_store, current_workflow_id=current_workflow_id,
        depth=depth, variables=variables,
    ):
        if event["type"] in {"node_completed", "node_failed", "node_skipped"}:
            steps.append(dict(event["step"]))
        if event["type"] == "node_failed":
            raise WorkflowExecutionError(str(event.get("message", "工作流执行失败")), steps)
        if event["type"] == "run_completed":
            output = str(event["output"])
            steps = list(event["steps"])
    return output, steps


class WorkflowExecutionError(RuntimeError):
    def __init__(self, message: str, steps: list[dict[str, object]]) -> None:
        super().__init__(message)
        self.steps = steps


def _execute_node(
    node: WorkflowNode,
    parents: list[object],
    input_text: str,
    knowledge_roots: list[Path],
    public_knowledge_root: Path | None,
    registry: MCPClientRegistry,
    todo_store: TodoStoreProtocol | None = None,
    workflow_store: WorkflowLookup | None = None,
    current_workflow_id: str | None = None,
    depth: int = 0,
    variables: dict[str, object] | None = None,
) -> tuple[object, str, str | None]:
    upstream = "\n\n".join(_as_text(value) for value in parents if value is not None)
    run_variables = variables if variables is not None else {}
    if node.type == "input":
        value = input_text.strip() or str(node.config.get("default", "")).strip()
        if not value:
            raise ValueError("请输入本次运行内容。")
        return value, f"接收 {len(value)} 个字符", None
    if not parents:
        raise ValueError(f"节点“{node.label}”没有上游输入。")

    if node.type == "knowledge":
        limit = max(1, min(int(node.config.get("limit", 3)), 10))
        result = search_knowledge(
            upstream[:300], limit=limit, roots=knowledge_roots,
            public_root=public_knowledge_root,
        )
        matches = result.get("results", [])
        context = "\n\n".join(
            f"[{item['source']} · 片段 {item['chunk']} · {item['score']} 分]\n{item['content']}"
            for item in matches if isinstance(item, dict)
        )
        return {"query": upstream, "context": context, "matches": matches}, f"命中 {len(matches)} 个知识片段", None

    if node.type == "agent":
        return _run_agent_node(node, upstream, registry, todo_store)

    if node.type == "llm":
        prompt = str(node.config.get("prompt", "请基于输入给出准确、简洁的回答。"))
        system = str(node.config.get("system", "你是企业知识助手，请依据上下文回答。"))
        role = str(node.config.get("role", "strong")).strip().lower()
        if role not in {"cheap", "strong", "default"}:
            role = "strong"
        model_override = node.config.get("model")
        response, _used = create_chat_completion(
            create_client(),
            role=role,  # type: ignore[arg-type]
            model=str(model_override) if model_override else None,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"任务：{prompt}\n\n上游输入：\n{upstream}"},
            ],
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("模型返回了空结果。")
        return content, "DeepSeek 推理完成", None

    if node.type == "mcp":
        tool_name = str(node.config.get("tool", "get_weather"))
        arguments = _tool_arguments(
            tool_name, node.config, input_text, upstream, run_variables
        )
        parsed = _call_registry_tool(registry, tool_name, arguments)
        return parsed, f"通过 MCP 调用 {tool_name}", None

    if node.type == "http":
        return _run_http_node(node, input_text, upstream, run_variables)

    if node.type == "transform":
        return _run_transform_node(node, parents, input_text, upstream, run_variables)

    if node.type == "foreach":
        return _run_foreach_node(
            node, parents, input_text, knowledge_roots, public_knowledge_root,
            registry, todo_store, workflow_store, current_workflow_id, depth,
            run_variables,
        )

    if node.type == "subworkflow":
        return _run_subworkflow_node(
            node, input_text, upstream, knowledge_roots, public_knowledge_root,
            registry, todo_store, workflow_store, current_workflow_id, depth,
            run_variables,
        )

    if node.type == "todo":
        if todo_store is None:
            raise ValueError("当前工作流没有 Todo 存储，无法添加任务。")
        title = _render_template(
            str(node.config.get("title", "")).strip() or "{{upstream}}",
            input_text=input_text,
            upstream=upstream,
            variables=run_variables,
        ).strip()
        todo = todo_store.add(title)
        return {"todo": todo}, f"已添加任务 #{todo['id']}：{todo['title']}", None

    if node.type == "condition":
        contains = str(node.config.get("contains", "")).strip()
        matched = contains.casefold() in upstream.casefold() if contains else bool(upstream.strip())
        branch = "true" if matched else "false"
        return {"matched": matched, "value": upstream, "rule": contains}, f"条件结果：{'是' if matched else '否'}", branch

    if node.type == "approval":
        return parents[-1], "已获得人工确认，继续执行", None

    return (parents[-1] if len(parents) == 1 else parents), "整理并输出最终结果", None


def _run_agent_node(
    node: WorkflowNode,
    upstream: str,
    registry: MCPClientRegistry,
    todo_store: TodoStoreProtocol | None = None,
) -> tuple[object, str, None]:
    configured = node.config.get("tools", ["search_knowledge", "get_weather"])
    enabled = {str(item) for item in configured} if isinstance(configured, list) else {"search_knowledge", "get_weather"}
    tools = _agent_tools(enabled, registry)
    skill_block, loaded_skills = _selected_skill_instructions(node.config)
    system = str(
        node.config.get(
            "system",
            "你是企业 Agent。先判断任务是否需要工具；需要时必须调用合适工具，得到结果后再生成最终中文回答。",
        )
    ) + skill_block
    prompt = str(node.config.get("prompt", "完成用户任务，并明确说明使用了哪些依据。"))
    messages: list[object] = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"任务要求：{prompt}\n\n用户输入：\n{upstream}"},
    ]
    used_tools: list[str] = []
    client = create_client()
    role = str(node.config.get("role", "strong")).strip().lower()
    if role not in {"cheap", "strong", "default"}:
        role = "strong"
    model_override = node.config.get("model")
    for _ in range(max(1, min(int(node.config.get("max_rounds", 4)), 8))):
        kwargs: dict[str, object] = {
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        response, _used = create_chat_completion(
            client,
            role=role,  # type: ignore[arg-type]
            model=str(model_override) if model_override else None,
            **kwargs,
        )
        message = response.choices[0].message
        calls = message.tool_calls or []
        if not calls:
            if not message.content:
                raise RuntimeError("Agent 返回了空结果。")
            summary = f"Agent 完成分析，调用 {len(used_tools)} 个工具"
            if loaded_skills:
                summary += f"，加载 {len(loaded_skills)} 个 Skill"
            return {
                "answer": message.content,
                "tools_used": used_tools,
                "skills_loaded": loaded_skills,
            }, summary, None
        messages.append(message)
        for call in calls:
            tool_name = call.function.name
            arguments = json.loads(call.function.arguments or "{}")
            if tool_name in WORKFLOW_LOCAL_TOOLS:
                result = _call_local_tool(tool_name, arguments, todo_store)
            else:
                result = _call_registry_tool(registry, tool_name, arguments)
            used_tools.append(tool_name)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)})
    raise RuntimeError("Agent 工具调用轮次过多，已停止。")


def _agent_tools(enabled: set[str], registry: MCPClientRegistry) -> list[dict[str, object]]:
    tools: list[dict[str, object]] = []
    for tool in ALL_TOOLS:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name"))
        if name in enabled and name in WORKFLOW_LOCAL_TOOLS:
            tools.append(tool)
    for tool in registry.tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        if str(function.get("name")) in enabled:
            tools.append(tool)
    return tools


def _call_local_tool(
    name: str,
    arguments: dict[str, object],
    todo_store: TodoStoreProtocol | None,
) -> object:
    if name in {"add_todo", "list_todos"} and todo_store is None:
        raise ValueError("当前工作流没有 Todo 存储，无法调用本地 Todo 工具。")
    parsed = json.loads(
        run_tool(name, json.dumps(arguments, ensure_ascii=False), todo_store=todo_store)
    )
    if isinstance(parsed, dict) and "error" in parsed:
        raise RuntimeError(str(parsed["error"]))
    return parsed


def _selected_skill_instructions(config: dict[str, object]) -> tuple[str, list[str]]:
    selected = config.get("skills", [])
    if not isinstance(selected, list) or not selected:
        return "", []
    parts: list[str] = []
    loaded: list[str] = []
    for raw_name in selected:
        name = str(raw_name).strip()
        if not name:
            continue
        try:
            skill = load_skill(name)
        except ValueError as error:
            raise ValueError(f"Agent 节点配置了无效 Skill：{error}") from error
        loaded.append(skill.name)
        parts.append(f"## Skill: {skill.name}\n{skill.body}")
    if not parts:
        return "", []
    return "\n\n请遵循以下已启用的 Agent Skills：\n\n" + "\n\n".join(parts), loaded


def _tool_arguments(
    name: str,
    config: dict[str, object],
    input_text: str,
    upstream: str,
    variables: dict[str, object],
) -> dict[str, object]:
    raw_arguments = str(config.get("arguments_json", "")).strip()
    if raw_arguments:
        parsed = json.loads(
            _render_template(
                raw_arguments,
                input_text=input_text,
                upstream=upstream,
                variables=variables,
            )
        )
        if not isinstance(parsed, dict):
            raise ValueError("MCP 参数必须是 JSON 对象。")
        return parsed
    if name == "get_weather":
        location = str(config.get("location", "")).strip()
        return {
            "location": _render_template(
                location or "{{input}}",
                input_text=input_text,
                upstream=upstream,
                variables=variables,
            ),
            "days": max(1, min(int(config.get("days", 3)), 7)),
        }
    if name == "search_knowledge":
        return {
            "query": upstream[:300],
            "limit": max(1, min(int(config.get("limit", 3)), 10)),
        }
    return {}


def _call_registry_tool(
    registry: MCPClientRegistry, name: str, arguments: dict[str, object]
) -> object:
    raw = registry.call_tool(name, json.dumps(arguments, ensure_ascii=False))
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and "error" in parsed:
        raise RuntimeError(str(parsed["error"]))
    return parsed


def _final_text(value: object) -> str:
    if isinstance(value, dict) and isinstance(value.get("answer"), str):
        return str(value["answer"])
    if isinstance(value, dict) and isinstance(value.get("output"), str) and "workflow" in value:
        return str(value["output"])
    if isinstance(value, dict) and isinstance(value.get("text"), str) and "status_code" in value:
        return str(value["text"])
    return _as_text(value)


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def _mapped_parents(node: WorkflowNode, parents: list[object]) -> list[object]:
    path = str(node.config.get("map_from", "")).strip()
    if not path or not parents:
        return parents
    source = parents[0] if len(parents) == 1 else parents
    return [_lookup_path(source, path)]


def _save_variable(
    node: WorkflowNode, value: object, variables: dict[str, object]
) -> None:
    name = str(node.config.get("save_as", "")).strip()
    if not name:
        return
    if not _VARIABLE_NAME.fullmatch(name):
        raise ValueError("变量名只能使用字母、数字和下划线，并以字母或下划线开头。")
    variables[name] = value


def _lookup_path(value: object, path: str) -> object:
    current: object = value
    if isinstance(current, str):
        try:
            current = json.loads(current)
        except json.JSONDecodeError:
            pass
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as error:
                raise ValueError(f"找不到路径 {path}。") from error
        elif isinstance(current, dict):
            if part not in current:
                raise ValueError(f"找不到路径 {path}。")
            current = current[part]
        else:
            raise ValueError(f"找不到路径 {path}。")
    return current


def _render_template(
    template: str,
    *,
    input_text: str = "",
    upstream: str = "",
    variables: dict[str, object] | None = None,
    item: object | None = None,
    extra: dict[str, object] | None = None,
) -> str:
    context: dict[str, object] = {
        "input": input_text,
        "upstream": upstream,
        "item": item if item is not None else "",
        "vars": variables or {},
        "json": _maybe_json(upstream),
    }
    if extra:
        context.update(extra)

    def replace(match: re.Match[str]) -> str:
        value = _lookup_path(context, match.group(1))
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    return _TEMPLATE_PATTERN.sub(replace, template)


def _maybe_json(value: object) -> object:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def _run_http_node(
    node: WorkflowNode,
    input_text: str,
    upstream: str,
    variables: dict[str, object],
) -> tuple[object, str, None]:
    method = str(node.config.get("method", "GET")).strip().upper() or "GET"
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("HTTP 方法只支持 GET、POST、PUT、PATCH、DELETE。")
    url = _render_template(
        str(node.config.get("url", "")).strip(),
        input_text=input_text,
        upstream=upstream,
        variables=variables,
    )
    if not url:
        raise ValueError("请填写 HTTP 请求地址。")
    _ensure_public_http_url(url)
    headers = _parse_headers(
        _render_template(
            str(node.config.get("headers", "")),
            input_text=input_text,
            upstream=upstream,
            variables=variables,
        )
    )
    body = _render_template(
        str(node.config.get("body", "")),
        input_text=input_text,
        upstream=upstream,
        variables=variables,
    )
    timeout = max(2.0, min(float(node.config.get("timeout", 8)), 15.0))
    response = _http_request(
        method, url, headers, body.encode("utf-8") if body else None, timeout
    )
    text = response.text[:MAX_HTTP_BODY_BYTES]
    payload: dict[str, object] = {
        "status_code": response.status_code,
        "url": url,
        "text": text,
    }
    content_type = response.headers.get("content-type", "")
    if "json" in content_type.lower() or text[:1] in {"{", "["}:
        try:
            payload["json"] = json.loads(text)
        except json.JSONDecodeError:
            pass
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}：{text[:180]}")
    return payload, f"HTTP {method} {response.status_code}", None


def _http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> httpx.Response:
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        response = client.request(method, url, headers=headers or None, content=body)
    if len(response.content) > MAX_HTTP_BODY_BYTES:
        raise ValueError("HTTP 响应过大，已拒绝保存。")
    return response


def _parse_headers(raw: str) -> dict[str, str]:
    text = raw.strip()
    if not text:
        return {}
    if text.startswith("{"):
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("请求头必须是 JSON 对象。")
        return {str(key): str(value) for key, value in parsed.items()}
    headers: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def _ensure_public_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("HTTP 节点只允许 http 或 https 地址。")
    if parsed.username or parsed.password:
        raise ValueError("HTTP 地址不能包含账号密码。")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("HTTP 地址缺少主机名。")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".localhost"):
        raise ValueError("不允许访问内网或本机地址。")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as error:
        raise ValueError(f"无法解析主机名：{host}") from error
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("不允许访问内网或保留地址。")


def _run_transform_node(
    node: WorkflowNode,
    parents: list[object],
    input_text: str,
    upstream: str,
    variables: dict[str, object],
) -> tuple[object, str, None]:
    mode = str(node.config.get("mode", "template")).strip() or "template"
    source = parents[0] if len(parents) == 1 else parents
    if mode == "template":
        template = str(node.config.get("template", "{{upstream}}"))
        rendered = _render_template(
            template, input_text=input_text, upstream=upstream, variables=variables
        )
        return rendered, "已按模板转换数据", None
    if mode == "json_path":
        path = str(node.config.get("path", "")).strip()
        if not path:
            raise ValueError("请填写 JSON 取值路径。")
        value = _lookup_path(source, path)
        return value, f"已读取 {path}", None
    if mode == "split":
        separator = str(node.config.get("separator", "\n"))
        items = [
            item.strip()
            for item in _as_text(source).split(separator)
            if item.strip()
        ]
        return items, f"已拆成 {len(items)} 项", None
    if mode == "join":
        separator = str(node.config.get("separator", "\n"))
        items = source if isinstance(source, list) else _extract_items(source, "", "\n")
        text = separator.join(_as_text(item) if not isinstance(item, str) else item for item in items)
        return text, f"已合并 {len(items)} 项", None
    if mode == "json_parse":
        if isinstance(source, (dict, list)):
            return source, "已解析 JSON", None
        return json.loads(_as_text(source)), "已解析 JSON", None
    raise ValueError("不支持的转换方式。")


def _run_foreach_node(
    node: WorkflowNode,
    parents: list[object],
    input_text: str,
    knowledge_roots: list[Path],
    public_knowledge_root: Path | None,
    registry: MCPClientRegistry,
    todo_store: TodoStoreProtocol | None,
    workflow_store: WorkflowLookup | None,
    current_workflow_id: str | None,
    depth: int,
    variables: dict[str, object],
) -> tuple[object, str, None]:
    source = parents[0] if len(parents) == 1 else parents
    items = _extract_items(
        source,
        str(node.config.get("path", "")).strip(),
        str(node.config.get("separator", "\n")),
    )
    limit = max(1, min(int(node.config.get("max_items", 10)), MAX_FOREACH_ITEMS))
    selected = items[:limit]
    template = str(node.config.get("template", "{{item}}"))
    child_id = str(node.config.get("workflow_id", "")).strip()
    results: list[object] = []
    for item in selected:
        rendered = _render_template(
            template,
            input_text=input_text,
            upstream=_as_text(item) if not isinstance(item, str) else item,
            variables=variables,
            item=item,
        )
        if child_id:
            output, _steps = _execute_child_workflow(
                child_id,
                rendered,
                knowledge_roots,
                public_knowledge_root,
                registry,
                todo_store,
                workflow_store,
                current_workflow_id,
                depth,
                variables,
            )
            results.append(output)
        else:
            if rendered[:1] in {"{", "["}:
                try:
                    results.append(json.loads(rendered))
                except json.JSONDecodeError:
                    results.append(rendered)
            else:
                results.append(rendered)
    join_with = node.config.get("join")
    if isinstance(join_with, str) and join_with != "":
        text = join_with.join(
            item if isinstance(item, str) else _as_text(item) for item in results
        )
        return text, f"已批处理 {len(results)} 项并合并", None
    return {"items": selected, "results": results}, f"已批处理 {len(results)} 项", None


def _run_subworkflow_node(
    node: WorkflowNode,
    input_text: str,
    upstream: str,
    knowledge_roots: list[Path],
    public_knowledge_root: Path | None,
    registry: MCPClientRegistry,
    todo_store: TodoStoreProtocol | None,
    workflow_store: WorkflowLookup | None,
    current_workflow_id: str | None,
    depth: int,
    variables: dict[str, object],
) -> tuple[object, str, None]:
    child_id = str(node.config.get("workflow_id", "")).strip()
    child_input = _render_template(
        str(node.config.get("input_template", "{{upstream}}") or "{{upstream}}"),
        input_text=input_text,
        upstream=upstream,
        variables=variables,
    )
    output, steps = _execute_child_workflow(
        child_id,
        child_input,
        knowledge_roots,
        public_knowledge_root,
        registry,
        todo_store,
        workflow_store,
        current_workflow_id,
        depth,
        variables,
    )
    name = ""
    if workflow_store is not None and child_id:
        try:
            name = str(workflow_store.get(child_id).get("name", ""))
        except Exception:
            name = ""
    return {
        "workflow_id": child_id,
        "workflow": name,
        "output": output,
        "step_count": len(steps),
    }, f"子工作流「{name or child_id}」已完成", None


def _execute_child_workflow(
    child_id: str,
    child_input: str,
    knowledge_roots: list[Path],
    public_knowledge_root: Path | None,
    registry: MCPClientRegistry,
    todo_store: TodoStoreProtocol | None,
    workflow_store: WorkflowLookup | None,
    current_workflow_id: str | None,
    depth: int,
    variables: dict[str, object],
) -> tuple[str, list[dict[str, object]]]:
    if workflow_store is None:
        raise ValueError("当前环境无法读取子工作流。")
    if not child_id:
        raise ValueError("请选择要调用的子工作流。")
    if child_id == current_workflow_id:
        raise ValueError("子工作流不能调用自己。")
    if depth >= MAX_SUBWORKFLOW_DEPTH:
        raise ValueError(f"子工作流嵌套不能超过 {MAX_SUBWORKFLOW_DEPTH} 层。")
    saved = workflow_store.get(child_id)
    child = WorkflowDefinition.model_validate(saved["definition"])
    return execute_workflow(
        child,
        child_input,
        knowledge_roots,
        public_knowledge_root,
        registry,
        todo_store=todo_store,
        workflow_store=workflow_store,
        current_workflow_id=child_id,
        depth=depth + 1,
        variables=dict(variables),
    )


def _extract_items(value: object, path: str, separator: str) -> list[object]:
    current = _lookup_path(value, path) if path else value
    if isinstance(current, list):
        return current
    if isinstance(current, dict):
        for key in ("items", "results", "data"):
            nested = current.get(key)
            if isinstance(nested, list):
                return nested
        return [current]
    text = _as_text(current).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    return [item.strip() for item in text.split(separator) if item.strip()]


def build_workflow_runtime(database_path: Path, user_id: str) -> dict[str, object]:
    """Worker 与 API 共用的工作流运行时：知识目录、MCP 和当前用户 Todo。"""
    from hello_agent.database import SqliteTodoStore
    from hello_agent.knowledge_documents import (
        public_knowledge_dir,
        user_knowledge_dir,
    )
    from hello_agent.mcp_client import build_user_mcp_registry

    public_dir = str(public_knowledge_dir())
    user_dir = str(user_knowledge_dir(user_id))
    return {
        "knowledge_roots": [Path(user_dir), Path(public_dir)],
        "public_knowledge_root": Path(public_dir),
        "mcp_client": build_user_mcp_registry(database_path, user_id),
        "todo_store": SqliteTodoStore(database_path, session_id=user_id),
    }


def _topological_order(nodes: list[WorkflowNode], edges: list[WorkflowEdge]) -> list[str]:
    indegree = {node.id: 0 for node in nodes}
    outgoing: dict[str, list[str]] = {node.id: [] for node in nodes}
    for edge in edges:
        indegree[edge.target] += 1
        outgoing[edge.source].append(edge.target)
    queue = [node.id for node in nodes if indegree[node.id] == 0]
    order: list[str] = []
    while queue:
        node_id = queue.pop(0)
        order.append(node_id)
        for target in outgoing[node_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(order) != len(nodes):
        raise ValueError("工作流不能包含循环连线。")
    return order
