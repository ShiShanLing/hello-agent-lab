"""会话级聊天附件：文本抽取与本地 OCR，供 Agent 分析。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from hello_agent.knowledge_documents import (
    ATTACHMENT_ALLOWED_SUFFIXES,
    MAX_UPLOAD_BYTES,
    extract_document_payload,
    knowledge_upload_root,
)


MAX_ATTACHMENTS_PER_SESSION = 5
INLINE_CONTEXT_CHARS = 12_000
READ_DEFAULT_CHARS = 8_000
PREVIEW_CHARS = 240

ATTACHMENT_INSTRUCTIONS = (
    "用户可能在当前会话上传了聊天附件（Markdown/TXT/PDF/Word，或 PNG/JPG/WEBP 图片）。"
    "系统会附带附件目录；需要全文时使用 list_chat_attachments / read_chat_attachment。"
    "文字版 PDF 优先直接抽文本；扫描件 PDF 与图片使用本地 Tesseract OCR。"
    "OCR 结果可能有错字，分析时请谨慎并注明文件名；不要假装读过未提供的文件。"
)

LIST_CHAT_ATTACHMENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_chat_attachments",
        "description": "列出当前会话已上传的聊天附件及摘要。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

READ_CHAT_ATTACHMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "read_chat_attachment",
        "description": "读取指定聊天附件的正文片段，用于总结、提取或问答。",
        "parameters": {
            "type": "object",
            "properties": {
                "attachment_id": {
                    "type": "string",
                    "description": "附件 ID",
                },
                "start": {
                    "type": "integer",
                    "description": "起始字符位置，默认 0",
                },
                "limit": {
                    "type": "integer",
                    "description": "读取长度，默认 8000，最大 12000",
                },
            },
            "required": ["attachment_id"],
            "additionalProperties": False,
        },
    },
}

CHAT_ATTACHMENT_TOOLS = [LIST_CHAT_ATTACHMENTS_TOOL, READ_CHAT_ATTACHMENT_TOOL]
CHAT_ATTACHMENT_TOOL_NAMES = {
    "list_chat_attachments",
    "read_chat_attachment",
}


class ChatAttachmentStoreProtocol(Protocol):
    def list(self, session_id: str) -> list[dict[str, object]]: ...

    def get(self, attachment_id: str) -> dict[str, object]: ...

    def read_text(self, attachment_id: str) -> str: ...

    def create(
        self,
        *,
        session_id: str,
        original_name: str,
        content: bytes,
    ) -> dict[str, object]: ...

    def delete(self, attachment_id: str) -> dict[str, object]: ...


def chat_attachment_root() -> Path:
    configured = os.getenv("CHAT_ATTACHMENT_DIR")
    if configured:
        return Path(configured).resolve()
    return (knowledge_upload_root().parent / "chat_attachments").resolve()


def user_attachment_dir(user_id: str) -> Path:
    root = chat_attachment_root()
    directory = (root / user_id).resolve()
    try:
        directory.relative_to(root)
    except ValueError as error:
        raise ValueError("附件账号目录不合法。") from error
    return directory


def extract_attachment_text(filename: str, content: bytes) -> tuple[str, str]:
    """聊天附件抽取：允许图片，返回 (text, method)。"""
    suffix = Path(filename or "").suffix.lower()
    if suffix and suffix not in ATTACHMENT_ALLOWED_SUFFIXES:
        raise ValueError(
            "仅支持 Markdown、TXT、PDF、Word（.docx）以及 PNG/JPG/WEBP 图片。"
        )
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("单个文件不能超过 8 MB。")
    text, method = extract_document_payload(
        filename,
        content,
        allow_images=True,
        enable_ocr=None,
    )
    return text, method


def format_attachment_context(attachments: list[dict[str, object]]) -> str | None:
    if not attachments:
        return None
    lines = ["本会话聊天附件（文字抽取 / 本地 OCR）："]
    total_chars = 0
    bodies: list[str] = []
    for item in attachments:
        name = str(item.get("original_name") or "未命名")
        attachment_id = str(item.get("id") or "")
        char_count = int(item.get("char_count") or 0)
        preview = str(item.get("preview") or "")
        method = str(item.get("extraction_method") or "")
        method_note = f"，{method}" if method else ""
        lines.append(
            f"- {name}（id={attachment_id}，约 {char_count} 字{method_note}）"
            + (f"：{preview}" if preview else "")
        )
        total_chars += char_count
        text = str(item.get("text") or "")
        if text:
            bodies.append(f"### 附件：{name}\n\n{text}")
    if total_chars <= INLINE_CONTEXT_CHARS and bodies:
        lines.append("\n以下为附件全文：\n")
        lines.extend(bodies)
    else:
        lines.append(
            "\n附件较长，上文仅为目录与预览；需要细节时请调用 read_chat_attachment。"
        )
    return "\n".join(lines)


def run_chat_attachment_tool(
    name: str,
    arguments: str,
    store: ChatAttachmentStoreProtocol,
    session_id: str,
) -> str:
    if name == "list_chat_attachments":
        items = store.list(session_id)
        return json.dumps(
            {
                "attachments": [
                    {
                        "id": item["id"],
                        "original_name": item["original_name"],
                        "file_type": item["file_type"],
                        "char_count": item["char_count"],
                        "preview": item.get("preview") or "",
                        "extraction_method": item.get("extraction_method") or "",
                    }
                    for item in items
                ]
            },
            ensure_ascii=False,
        )
    if name == "read_chat_attachment":
        try:
            payload = json.loads(arguments or "{}")
            if not isinstance(payload, dict):
                raise ValueError("参数必须是 JSON 对象。")
            attachment_id = str(payload.get("attachment_id") or "").strip()
            if not attachment_id:
                raise ValueError("请提供 attachment_id。")
            start = max(0, int(payload.get("start") or 0))
            limit = int(payload.get("limit") or READ_DEFAULT_CHARS)
            limit = max(1, min(limit, INLINE_CONTEXT_CHARS))
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            return json.dumps({"error": str(error)}, ensure_ascii=False)
        try:
            detail = store.get(attachment_id)
            if str(detail.get("session_id")) != session_id:
                raise ValueError("找不到该会话下的附件。")
            text = store.read_text(attachment_id)
        except ValueError as error:
            return json.dumps({"error": str(error)}, ensure_ascii=False)
        chunk = text[start : start + limit]
        return json.dumps(
            {
                "id": detail["id"],
                "original_name": detail["original_name"],
                "start": start,
                "limit": limit,
                "total_chars": len(text),
                "text": chunk,
                "truncated": start + limit < len(text),
            },
            ensure_ascii=False,
        )
    return json.dumps({"error": f"未知附件工具：{name}"}, ensure_ascii=False)
