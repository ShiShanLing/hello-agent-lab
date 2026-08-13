"""面向 Markdown/TXT 的轻量级本地知识库检索。"""

import os
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"
ALLOWED_SUFFIXES = {".md", ".txt"}
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CHUNK_CHARACTERS = 800
CHUNK_OVERLAP_CHARACTERS = 100
_ENGLISH_TERM = re.compile(r"[a-z0-9_]+")
_CHINESE_SEQUENCE = re.compile(r"[\u3400-\u9fff]+")


@dataclass(frozen=True)
class KnowledgeChunk:
    source: str
    index: int
    content: str


@dataclass(frozen=True)
class KnowledgeFile:
    path: Path
    source: str
    size_bytes: int


def configured_knowledge_dir() -> Path:
    return Path(os.getenv("KNOWLEDGE_DIR", str(DEFAULT_KNOWLEDGE_DIR))).resolve()


def configured_knowledge_dirs() -> list[Path]:
    configured = os.getenv("KNOWLEDGE_DIRS", "").strip()
    if not configured:
        return [configured_knowledge_dir()]
    unique: list[Path] = []
    seen: set[Path] = set()
    for raw in configured.split(os.pathsep):
        if not raw.strip():
            continue
        path = Path(raw).resolve()
        if path not in seen:
            unique.append(path)
            seen.add(path)
    return unique or [configured_knowledge_dir()]


def list_knowledge_files(
    roots: list[Path] | None = None,
    public_root: Path | None = None,
) -> list[dict[str, object]]:
    return [
        {
            "source": file.source,
            "size_bytes": file.size_bytes,
        }
        for file in _knowledge_files(roots, public_root)
    ]


def search_knowledge(
    query: str,
    limit: int = 5,
    roots: list[Path] | None = None,
    public_root: Path | None = None,
) -> dict[str, object]:
    query = query.strip()
    if not query:
        raise ValueError("搜索内容不能为空。")
    if len(query) > 300:
        raise ValueError("搜索内容不能超过 300 个字符。")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("返回数量必须是 1 到 10 之间的整数。")

    files = _knowledge_files(roots, public_root)
    chunks = _load_chunks(files)
    query_terms = _search_terms(query)
    normalized_query = query.casefold()
    scored: list[tuple[float, KnowledgeChunk]] = []

    for chunk in chunks:
        content = chunk.content.casefold()
        source = chunk.source.casefold()
        score = 0.0
        if normalized_query in content:
            score += 20.0
        for term in query_terms:
            occurrences = content.count(term)
            if occurrences:
                score += min(occurrences, 4) * max(1.0, len(term) / 2)
            if term in source:
                score += 3.0
        if score > 0:
            scored.append((score, chunk))

    scored.sort(key=lambda item: (-item[0], item[1].source, item[1].index))
    return {
        "query": query,
        "results": [
            {
                "source": chunk.source,
                "chunk": chunk.index,
                "content": chunk.content,
                "score": round(score, 2),
            }
            for score, chunk in scored[:limit]
        ],
        "document_count": len(files),
        "message": (
            None
            if scored
            else "本地知识库中没有找到相关内容，请换一个关键词或添加资料。"
        ),
    }


def _is_allowed_file(path: Path, root: Path) -> bool:
    if not path.is_file() or path.is_symlink() or path.suffix.lower() not in ALLOWED_SUFFIXES:
        return False
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return path.stat().st_size <= MAX_FILE_BYTES


def _knowledge_files(
    roots: list[Path] | None = None,
    public_root: Path | None = None,
) -> list[KnowledgeFile]:
    files: list[KnowledgeFile] = []
    if public_root is None:
        public_dir = os.getenv("PUBLIC_KNOWLEDGE_DIR", "").strip()
        public_path = Path(public_dir).resolve() if public_dir else None
    else:
        public_path = public_root.resolve()
    active_roots = [root.resolve() for root in (roots or configured_knowledge_dirs())]
    multiple_roots = len(active_roots) > 1
    for root in active_roots:
        if not root.exists():
            continue
        label = "公共知识库" if public_path and root == public_path else "个人知识库"
        for path in sorted(root.rglob("*")):
            if not _is_allowed_file(path, root):
                continue
            relative = path.relative_to(root).as_posix()
            source = f"{label}/{relative}" if multiple_roots else relative
            files.append(
                KnowledgeFile(
                    path=path.resolve(),
                    source=source,
                    size_bytes=path.stat().st_size,
                )
            )
    return files


def _load_chunks(files: list[KnowledgeFile]) -> list[KnowledgeChunk]:
    chunks = []
    for file in files:
        text = file.path.read_text(encoding="utf-8")
        display_source = re.sub(r"(^|/)[0-9a-f]{8}-", r"\1", file.source)
        for index, content in enumerate(split_text(text), start=1):
            chunks.append(KnowledgeChunk(display_source, index, content))
    return chunks


def split_text(text: str) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > MAX_CHUNK_CHARACTERS:
            if current:
                chunks.append(current)
                current = ""
            step = MAX_CHUNK_CHARACTERS - CHUNK_OVERLAP_CHARACTERS
            chunks.extend(
                paragraph[start : start + MAX_CHUNK_CHARACTERS]
                for start in range(0, len(paragraph), step)
            )
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= MAX_CHUNK_CHARACTERS:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


def _search_terms(text: str) -> set[str]:
    normalized = text.casefold()
    terms = set(_ENGLISH_TERM.findall(normalized))
    for sequence in _CHINESE_SEQUENCE.findall(normalized):
        if len(sequence) >= 2:
            terms.add(sequence)
            terms.update(
                sequence[index : index + 2]
                for index in range(len(sequence) - 1)
            )
    return {term for term in terms if term}
