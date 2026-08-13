"""本地 Tesseract OCR。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from hello_agent.knowledge_documents import extract_document_payload
from hello_agent.ocr import ocr_image_bytes, ocr_status


def _png_with_text(text: str = "Hello OCR") -> bytes:
    image = Image.new("RGB", (640, 160), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    # 放大默认字体渲染，减少低分辨率误识别
    draw.text((24, 48), text, fill=(0, 0, 0))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_ocr_status_reports_languages() -> None:
    status = ocr_status()
    assert "languages" in status
    assert "enabled" in status


def test_ocr_image_bytes_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCR_ENABLED", raising=False)
    content = _png_with_text("AB12")
    text = ocr_image_bytes(content)
    assert text.strip()


def test_extract_image_attachment_uses_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCR_ENABLED", raising=False)
    monkeypatch.setattr(
        "hello_agent.knowledge_documents.ocr_image_bytes",
        lambda _content: "Agent Lab",
    )
    text, method = extract_document_payload(
        "note.png",
        _png_with_text("ignored"),
        allow_images=True,
    )
    assert method == "ocr"
    assert "Agent Lab" in text
    assert "抽取方式：OCR" in text


def test_extract_image_rejected_for_knowledge() -> None:
    with pytest.raises(ValueError, match="聊天附件"):
        extract_document_payload("note.png", _png_with_text("x"), allow_images=False)


def test_pdf_ocr_fallback_when_native_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OCR_ENABLED", raising=False)

    def fake_ocr_pdf(_content: bytes) -> str:
        return "## 第 1 页\n\n扫描件正文"

    monkeypatch.setattr(
        "hello_agent.knowledge_documents.ocr_pdf_bytes",
        fake_ocr_pdf,
    )
    monkeypatch.setattr(
        "hello_agent.knowledge_documents._extract_text",
        lambda _content, _suffix: "",
    )
    text, method = extract_document_payload("scan.pdf", b"%PDF-fake")
    assert method == "ocr"
    assert "扫描件正文" in text
