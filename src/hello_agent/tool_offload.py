"""大体积工具结果卸载：上下文只留摘要与引用，全文按需取回。"""

from __future__ import annotations

import json
from typing import Protocol

OFFLOAD_THRESHOLD_CHARS = 4_000
PREVIEW_CHARS = 480
MAX_STORED_CHARS = 200_000
FETCH_TOOL_NAME = "fetch_tool_result"

FETCH_TOOL_RESULT_TOOL = {
    "type": "function",
    "function": {
        "name": FETCH_TOOL_NAME,
        "description": (
            "读取先前被卸载的完整工具结果。当工具返回 offloaded=true 且带有 ref 时使用；"
            "不要猜测全文内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "卸载结果中的 ref 标识",
                },
            },
            "required": ["ref"],
            "additionalProperties": False,
        },
    },
}

OFFLOAD_INSTRUCTIONS = (
    "部分工具结果可能被系统卸载为摘要："
    "内容含 offloaded=true 与 ref 时，上下文只有 preview；"
    "若回答需要完整字段或长文本，必须先调用 fetch_tool_result(ref)。"
)


class ToolResultStoreProtocol(Protocol):
    def put(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> str: ...

    def get(self, ref: str) -> str | None: ...


def should_offload(content: str, tool_name: str) -> bool:
    if tool_name == FETCH_TOOL_NAME:
        return False
    if not content or len(content) < OFFLOAD_THRESHOLD_CHARS:
        return False
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return True
    return not (isinstance(parsed, dict) and parsed.get("offloaded") is True)


def build_offload_stub(
    *,
    ref: str,
    tool_name: str,
    content: str,
) -> str:
    preview = content[:PREVIEW_CHARS]
    if len(content) > PREVIEW_CHARS:
        preview += "…"
    payload: dict[str, object] = {
        "offloaded": True,
        "ref": ref,
        "tool": tool_name,
        "bytes": len(content.encode("utf-8")),
        "chars": len(content),
        "preview": preview,
        "hint": "需要完整结果时调用 fetch_tool_result，传入 ref。",
    }
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and "error" in parsed:
        payload["error"] = parsed.get("error")
        payload["status"] = parsed.get("status", "failed")
    return json.dumps(payload, ensure_ascii=False)


def offload_tool_result(
    *,
    content: str,
    tool_name: str,
    tool_call_id: str,
    session_id: str,
    store: ToolResultStoreProtocol | None,
    local_cache: dict[str, str],
) -> str:
    """超过阈值则写入存储，返回给模型看的精简 JSON。"""
    if not should_offload(content, tool_name):
        return content
    stored = content if len(content) <= MAX_STORED_CHARS else content[:MAX_STORED_CHARS]
    ref: str | None = None
    if store is not None:
        try:
            ref = store.put(
                session_id=session_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                content=stored,
            )
        except Exception:
            ref = None
    if not ref:
        ref = f"local-{tool_call_id}"
        local_cache[ref] = stored
    else:
        local_cache[ref] = stored
    return build_offload_stub(ref=ref, tool_name=tool_name, content=stored)


def run_fetch_tool_result(
    arguments: str,
    *,
    store: ToolResultStoreProtocol | None,
    local_cache: dict[str, str],
) -> str:
    try:
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError("参数必须是 JSON 对象。")
        ref = str(parsed.get("ref") or "").strip()
        if not ref:
            raise ValueError("请提供 ref。")
    except (json.JSONDecodeError, ValueError) as error:
        return json.dumps({"error": str(error)}, ensure_ascii=False)

    content = local_cache.get(ref)
    if content is None and store is not None:
        content = store.get(ref)
        if content is not None:
            local_cache[ref] = content
    if content is None:
        return json.dumps(
            {"error": f"找不到卸载结果：{ref}", "ref": ref},
            ensure_ascii=False,
        )
    return content
