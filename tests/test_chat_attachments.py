"""聊天附件：文本抽取、会话注入与工具。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hello_agent.api import SessionManager, create_api
from hello_agent.app import AgentSession
from hello_agent.auth import SqliteAuthService
from hello_agent.chat_attachments import (
    format_attachment_context,
    run_chat_attachment_tool,
)
from hello_agent.database import SqliteChatAttachmentStore, SqliteConversationStore
from hello_agent.knowledge_documents import extract_document_text


def test_extract_document_text_from_markdown() -> None:
    text = extract_document_text("notes.md", b"# Hello\n\nworld")
    assert "Hello" in text
    assert "world" in text


def test_extract_empty_pdf_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from hello_agent import knowledge_documents as kd

    monkeypatch.setenv("OCR_ENABLED", "false")
    monkeypatch.setattr(kd, "_extract_text", lambda _content, _suffix: "")
    with pytest.raises(ValueError, match="OCR|扫描件|文字版|Tesseract"):
        extract_document_text("scan.pdf", b"%PDF-fake")


def test_attachment_store_and_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_ATTACHMENT_DIR", str(tmp_path / "attachments"))
    db = tmp_path / "agent.db"
    store = SqliteChatAttachmentStore(db, "user-1")
    created = store.create(
        session_id="session-1",
        original_name="brief.md",
        content="产品目标：提升留存。\n关键指标：次日留存。".encode("utf-8"),
    )
    assert created["original_name"] == "brief.md"
    assert int(created["char_count"]) > 0

    listed = store.list("session-1")
    assert len(listed) == 1
    assert listed[0]["preview"]

    context = format_attachment_context(store.list_with_text("session-1"))
    assert context is not None
    assert "brief.md" in context
    assert "留存" in context

    listed_json = run_chat_attachment_tool(
        "list_chat_attachments", "{}", store, "session-1"
    )
    assert "brief.md" in listed_json

    read_json = run_chat_attachment_tool(
        "read_chat_attachment",
        f'{{"attachment_id": "{created["id"]}", "limit": 100}}',
        store,
        "session-1",
    )
    assert "留存" in read_json

    store.delete(str(created["id"]))
    assert store.list("session-1") == []


def test_agent_includes_attachment_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_ATTACHMENT_DIR", str(tmp_path / "attachments"))
    db = tmp_path / "agent.db"
    attachment_store = SqliteChatAttachmentStore(db, "user-1")
    attachment_store.create(
        session_id="session-1",
        original_name="plan.txt",
        content="第一阶段完成登录。".encode("utf-8"),
    )
    agent = AgentSession(
        client=object(),  # type: ignore[arg-type]
        todo_store=object(),  # type: ignore[arg-type]
        conversation_store=SqliteConversationStore(db, "session-1"),
        attachment_store=attachment_store,
        auto_search_knowledge=False,
    )
    names = {tool["function"]["name"] for tool in agent.available_tools}  # type: ignore[index]
    assert "list_chat_attachments" in names
    assert "read_chat_attachment" in names
    context = agent._attachment_context()
    assert context is not None
    assert "plan.txt" in context


def test_chat_attachment_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_ATTACHMENT_DIR", str(tmp_path / "attachments"))
    db = tmp_path / "agent.db"
    auth = SqliteAuthService(db)
    manager = SessionManager(
        session_factory=lambda session_id, user_id=None: AgentSession(
            client=object(),  # type: ignore[arg-type]
            todo_store=object(),  # type: ignore[arg-type]
            conversation_store=SqliteConversationStore(db, session_id),
            attachment_store=(
                SqliteChatAttachmentStore(db, user_id) if user_id else None
            ),
            auto_search_knowledge=False,
        ),
        history_loader=lambda _session_id: [],
    )
    client = TestClient(create_api(session_manager=manager, auth_service=auth))
    registered = client.post(
        "/auth/register",
        json={
            "email": "attach@example.com",
            "password": "password123",
            "display_name": "附件用户",
        },
    )
    assert registered.status_code == 201, registered.text

    upload = client.post(
        "/chat/attachments",
        files={"file": ("todo.md", b"# Todo\n- ship attachments\n", "text/markdown")},
    )
    assert upload.status_code == 201, upload.text
    payload = upload.json()
    assert payload["original_name"] == "todo.md"
    session_id = payload["session_id"]

    listed = client.get(f"/chat/attachments?session_id={session_id}")
    assert listed.status_code == 200
    assert len(listed.json()["attachments"]) == 1

    deleted = client.delete(f"/chat/attachments/{payload['id']}")
    assert deleted.status_code == 204
    listed_after = client.get(f"/chat/attachments?session_id={session_id}")
    assert listed_after.json()["attachments"] == []
