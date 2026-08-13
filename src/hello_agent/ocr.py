"""本地 OCR：Tesseract + Pillow + pypdfium2。

适合本项目的原因：自托管、零 API 费用、附件不出服务器；中文用 chi_sim。
云 OCR / 识图大模型可日后作为可选增强，不作为默认路径。
"""

from __future__ import annotations

from io import BytesIO
import os
from typing import Literal

ExtractionMethod = Literal["text", "ocr"]

OCR_MAX_PDF_PAGES = 8
OCR_RENDER_SCALE = 2.0  # ~144–200 DPI 量级，兼顾速度与清晰度
DEFAULT_OCR_LANG = "chi_sim+eng"


def ocr_enabled() -> bool:
    return os.getenv("OCR_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def ocr_languages() -> str:
    configured = os.getenv("OCR_LANG", DEFAULT_OCR_LANG).strip()
    return configured or DEFAULT_OCR_LANG


def ocr_status() -> dict[str, object]:
    """供健康检查或排障使用。"""
    available = False
    version: str | None = None
    error: str | None = None
    if not ocr_enabled():
        return {
            "enabled": False,
            "available": False,
            "version": None,
            "languages": ocr_languages(),
            "error": "OCR_ENABLED=false",
        }
    try:
        import pytesseract

        version = str(pytesseract.get_tesseract_version())
        available = True
    except Exception as exc:  # noqa: BLE001 - 状态接口需要吞掉探测失败
        error = str(exc)
    return {
        "enabled": True,
        "available": available,
        "version": version,
        "languages": ocr_languages(),
        "error": error,
    }


def require_ocr_ready() -> None:
    if not ocr_enabled():
        raise ValueError("OCR 已关闭（OCR_ENABLED=false）。")
    try:
        import pytesseract
    except ImportError as error:
        raise ValueError(
            "未安装 pytesseract。请执行 pip install pytesseract Pillow pypdfium2。"
        ) from error
    try:
        pytesseract.get_tesseract_version()
    except Exception as error:  # noqa: BLE001
        raise ValueError(
            "未检测到 Tesseract。请安装 tesseract-ocr 及语言包 "
            f"（推荐：tesseract-ocr-chi-sim、tesseract-ocr-eng）。详情：{error}"
        ) from error


def ocr_image_bytes(content: bytes) -> str:
    """对单张图片做 OCR。"""
    require_ocr_ready()
    from PIL import Image, ImageOps, UnidentifiedImageError
    import pytesseract

    try:
        image = Image.open(BytesIO(content))
        image = ImageOps.exif_transpose(image)
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
    except UnidentifiedImageError as error:
        raise ValueError("无法识别的图片文件。") from error
    except OSError as error:
        raise ValueError("图片文件损坏或无法打开。") from error

    try:
        text = pytesseract.image_to_string(image, lang=ocr_languages())
    except pytesseract.TesseractError as error:
        raise ValueError(f"OCR 识别失败：{error}") from error
    cleaned = _normalize_ocr_text(text)
    if not cleaned:
        raise ValueError("OCR 没有识别到可用文字，请换更清晰的图片后重试。")
    return cleaned


def ocr_pdf_bytes(content: bytes) -> str:
    """将扫描件 PDF 渲染为图片后逐页 OCR。"""
    require_ocr_ready()
    try:
        import pypdfium2 as pdfium
    except ImportError as error:
        raise ValueError(
            "未安装 pypdfium2，无法渲染扫描件 PDF。请执行 pip install pypdfium2。"
        ) from error

    try:
        document = pdfium.PdfDocument(content)
    except Exception as error:  # noqa: BLE001
        raise ValueError("扫描件 PDF 打开失败，请确认文件没有损坏。") from error

    page_count = len(document)
    if page_count == 0:
        raise ValueError("PDF 没有可识别的页面。")
    limit = min(page_count, OCR_MAX_PDF_PAGES)
    pages: list[str] = []
    try:
        for index in range(limit):
            page = document[index]
            try:
                bitmap = page.render(scale=OCR_RENDER_SCALE)
                image = bitmap.to_pil()
            finally:
                page.close()
            page_text = _ocr_pil_image(image)
            if page_text:
                pages.append(f"## 第 {index + 1} 页\n\n{page_text}")
    finally:
        document.close()

    if not pages:
        raise ValueError("OCR 没有从 PDF 中识别到可用文字。")
    note = ""
    if page_count > OCR_MAX_PDF_PAGES:
        note = (
            f"\n\n> 说明：PDF 共 {page_count} 页，本次仅 OCR 前 {OCR_MAX_PDF_PAGES} 页。"
        )
    return "\n\n".join(pages) + note


def _ocr_pil_image(image: object) -> str:
    import pytesseract

    try:
        text = pytesseract.image_to_string(image, lang=ocr_languages())
    except pytesseract.TesseractError as error:
        raise ValueError(f"OCR 识别失败：{error}") from error
    return _normalize_ocr_text(text)


def _normalize_ocr_text(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    cleaned: list[str] = []
    blank_streak = 0
    for line in lines:
        if not line.strip():
            blank_streak += 1
            if blank_streak <= 1:
                cleaned.append("")
            continue
        blank_streak = 0
        cleaned.append(line.strip())
    return "\n".join(cleaned).strip()
