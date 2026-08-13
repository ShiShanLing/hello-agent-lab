"""用户知识文档的安全上传、文本解析和演示资料导入。"""

from io import BytesIO
import os
from pathlib import Path
import re
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader

from hello_agent.database import SqliteKnowledgeDocumentStore
from hello_agent.knowledge import split_text
from hello_agent.ocr import ExtractionMethod, ocr_enabled, ocr_image_bytes, ocr_pdf_bytes


DEFAULT_UPLOAD_ROOT = Path(__file__).resolve().parents[2] / "data" / "knowledge_uploads"
DEFAULT_DEMO_ROOT = Path(__file__).resolve().parents[2] / "demo_knowledge"
ALLOWED_SUFFIXES = {".md", ".txt", ".pdf", ".docx"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
ATTACHMENT_ALLOWED_SUFFIXES = ALLOWED_SUFFIXES | IMAGE_SUFFIXES
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_EXTRACTED_CHARACTERS = 1_500_000
PUBLIC_KNOWLEDGE_OWNER = "__public__"
MIN_NATIVE_PDF_CHARS = 20


def knowledge_upload_root() -> Path:
    configured = os.getenv("KNOWLEDGE_UPLOAD_DIR")
    if configured:
        return Path(configured).resolve()
    database_file = os.getenv("TODO_DATABASE_FILE")
    if database_file:
        return (Path(database_file).resolve().parent / "knowledge_uploads").resolve()
    return DEFAULT_UPLOAD_ROOT.resolve()


def user_knowledge_dir(user_id: str) -> Path:
    root = knowledge_upload_root()
    directory = (root / user_id).resolve()
    try:
        directory.relative_to(root)
    except ValueError as error:
        raise ValueError("知识库账号目录不合法。") from error
    return directory


def public_knowledge_dir() -> Path:
    return user_knowledge_dir(PUBLIC_KNOWLEDGE_OWNER)


def extract_document_text(
    filename: str,
    content: bytes,
    *,
    allow_images: bool = False,
    enable_ocr: bool | None = None,
) -> str:
    """从上传字节中抽取纯文本；必要时回退到本地 OCR。"""
    text, _method = extract_document_payload(
        filename,
        content,
        allow_images=allow_images,
        enable_ocr=enable_ocr,
    )
    return text


def extract_document_payload(
    filename: str,
    content: bytes,
    *,
    allow_images: bool = False,
    enable_ocr: bool | None = None,
) -> tuple[str, ExtractionMethod]:
    """返回正文与抽取方式（text / ocr）。"""
    original_name = Path(filename or "").name.strip()
    if not original_name:
        raise ValueError("文件名不能为空。")
    suffix = Path(original_name).suffix.lower()
    allowed = ATTACHMENT_ALLOWED_SUFFIXES if allow_images else ALLOWED_SUFFIXES
    if suffix not in allowed:
        if suffix in IMAGE_SUFFIXES and not allow_images:
            raise ValueError("知识库暂不支持直接上传图片；请在聊天附件中使用 OCR。")
        raise ValueError(
            "仅支持 Markdown、TXT、PDF、Word（.docx）"
            + (" 以及 PNG/JPG/WEBP 图片。" if allow_images else "。")
        )
    if not content:
        raise ValueError("不能上传空文件。")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("单个文件不能超过 8 MB。")

    use_ocr = ocr_enabled() if enable_ocr is None else enable_ocr
    method: ExtractionMethod = "text"
    if suffix in IMAGE_SUFFIXES:
        if not use_ocr:
            raise ValueError("图片识别需要开启 OCR（OCR_ENABLED=true）。")
        text = ocr_image_bytes(content)
        method = "ocr"
    else:
        text = _extract_text(content, suffix).strip()
        if not text and suffix == ".pdf" and use_ocr:
            text = ocr_pdf_bytes(content)
            method = "ocr"
        elif suffix == ".pdf" and use_ocr and len(text) < MIN_NATIVE_PDF_CHARS:
            # 极少数字符时也尝试 OCR，覆盖“几乎全是扫描页”的情况
            try:
                ocr_text = ocr_pdf_bytes(content)
            except ValueError:
                ocr_text = ""
            if len(ocr_text) > len(text):
                text = ocr_text
                method = "ocr"

    if not text:
        if suffix == ".pdf":
            if use_ocr:
                raise ValueError(
                    "没有从 PDF 中识别到可检索文字。"
                    "已尝试本地 OCR，仍失败；请换更清晰的扫描件或文字版 PDF。"
                )
            raise ValueError(
                "没有从 PDF 中识别到可检索文字。"
                "若是扫描件或图片型 PDF，请开启 OCR（安装 Tesseract），"
                "或上传文字版 PDF / TXT / Markdown。"
            )
        if suffix in IMAGE_SUFFIXES:
            raise ValueError("OCR 没有从图片中识别到可检索文字。")
        raise ValueError("没有从文件中识别到可检索文字。")
    if len(text) > MAX_EXTRACTED_CHARACTERS:
        raise ValueError("文档解析后的文字过多，请拆分后上传。")
    if method == "ocr":
        text = f"> 抽取方式：OCR（本地 Tesseract）\n\n{text}"
    return text, method


def save_knowledge_document(
    store: SqliteKnowledgeDocumentStore,
    user_id: str,
    filename: str,
    content: bytes,
    category: str = "未分类",
) -> dict[str, object]:
    original_name = Path(filename or "").name.strip()
    if not original_name:
        raise ValueError("文件名不能为空。")
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError("仅支持 Markdown、TXT、PDF 和 Word（.docx）文件。")
    if not content:
        raise ValueError("不能上传空文件。")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("单个文件不能超过 8 MB。")

    text = extract_document_text(original_name, content)
    directory = user_knowledge_dir(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    safe_stem = _safe_stem(Path(original_name).stem)
    stored_name = f"{uuid4().hex[:8]}-{safe_stem}.md"
    normalized = directory / stored_name
    normalized.write_text(
        f"# {original_name}\n\n{text}\n",
        encoding="utf-8",
    )
    try:
        return store.create(
            original_name=original_name,
            stored_name=stored_name,
            category=category,
            file_type=suffix.removeprefix("."),
            size_bytes=len(content),
            chunk_count=len(split_text(f"# {original_name}\n\n{text}")),
        )
    except Exception:
        normalized.unlink(missing_ok=True)
        raise


def delete_knowledge_document(
    store: SqliteKnowledgeDocumentStore,
    user_id: str,
    document_id: str,
) -> None:
    document = store.get(document_id)
    path = (user_knowledge_dir(user_id) / str(document["stored_name"])).resolve()
    try:
        path.relative_to(user_knowledge_dir(user_id))
    except ValueError as error:
        raise ValueError("知识文档路径不合法。") from error
    path.unlink(missing_ok=True)
    from hello_agent.vector_knowledge import delete_document_vectors

    delete_document_vectors(store.database_path, document_id)
    store.delete(document_id)


def read_knowledge_document_text(
    store: SqliteKnowledgeDocumentStore,
    user_id: str,
    document_id: str,
) -> str:
    document = store.get(document_id)
    path = (user_knowledge_dir(user_id) / str(document["stored_name"])).resolve()
    try:
        path.relative_to(user_knowledge_dir(user_id))
    except ValueError as error:
        raise ValueError("知识文档路径不合法。") from error
    if not path.exists():
        raise ValueError("知识文档原始内容不存在。")
    return path.read_text(encoding="utf-8")


def seed_demo_knowledge(
    store: SqliteKnowledgeDocumentStore,
    user_id: str,
) -> list[dict[str, object]]:
    demo_root = Path(
        os.getenv("DEMO_KNOWLEDGE_DIR", str(DEFAULT_DEMO_ROOT))
    ).resolve()
    if not demo_root.exists():
        return []
    existing_names = {str(item["original_name"]) for item in store.list()}
    created = []
    for path in sorted(demo_root.rglob("*.md")):
        if path.name in existing_names or path.is_symlink():
            continue
        category = path.parent.name if path.parent != demo_root else "演示资料"
        created.append(
            save_knowledge_document(
                store,
                user_id,
                path.name,
                path.read_bytes(),
                category,
            )
        )
    return created


def _extract_text(content: bytes, suffix: str) -> str:
    if suffix in {".md", ".txt"}:
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("文本文件必须使用 UTF-8 编码。") from error
    if suffix == ".pdf":
        try:
            reader = PdfReader(BytesIO(content))
            if reader.is_encrypted:
                raise ValueError("暂不支持加密 PDF。")
            pages = []
            for page_number, page in enumerate(reader.pages, start=1):
                page_text = (page.extract_text() or "").strip()
                if page_text:
                    pages.append(f"## 第 {page_number} 页\n\n{page_text}")
            return "\n\n".join(pages)
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("PDF 解析失败，请确认文件没有损坏。") from error
    if suffix == ".docx":
        try:
            with ZipFile(BytesIO(content)) as archive:
                xml = archive.read("word/document.xml")
        except (BadZipFile, KeyError) as error:
            raise ValueError("Word 文件解析失败，请上传有效的 .docx 文件。") from error
        root = ElementTree.fromstring(xml)
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = []
        for paragraph in root.iter(f"{namespace}p"):
            text = "".join(
                node.text or "" for node in paragraph.iter(f"{namespace}t")
            ).strip()
            if text:
                paragraphs.append(text)
        return "\n\n".join(paragraphs)
    raise ValueError("不支持的文件类型。")


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^\w\u3400-\u9fff-]+", "-", value, flags=re.UNICODE)
    return cleaned.strip("-_")[:80] or "document"
