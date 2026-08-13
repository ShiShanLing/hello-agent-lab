"""知识文档向量化、SQLite 向量存储与混合检索。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Lock
from typing import Protocol

from fastembed import TextEmbedding
import sqlite_vec
from sqlite_vec import serialize_float32

from hello_agent.database import SqliteKnowledgeDocumentStore
from hello_agent.knowledge import search_knowledge, split_text


DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
_MODEL_LOCK = Lock()
_MODELS: dict[tuple[str, str], TextEmbedding] = {}


class Embedder(Protocol):
    def embed(self, documents: Iterable[str], **kwargs: object) -> Iterable[object]: ...


def embedding_model_name() -> str:
    return os.getenv("KNOWLEDGE_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()


def _embedding_cache_dir(database_path: Path) -> Path:
    configured = os.getenv("KNOWLEDGE_EMBEDDING_CACHE_DIR", "").strip()
    path = Path(configured) if configured else database_path.parent / "embedding_models"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _get_embedder(database_path: Path) -> TextEmbedding:
    model_name = embedding_model_name()
    cache_dir = str(_embedding_cache_dir(database_path))
    key = (model_name, cache_dir)
    with _MODEL_LOCK:
        model = _MODELS.get(key)
        if model is None:
            model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
            _MODELS[key] = model
        return model


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path.resolve()), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_vector_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            source TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding BLOB NOT NULL,
            embedding_model TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(document_id, chunk_index)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_vector_owner "
        "ON knowledge_vector_chunks(user_id, document_id)"
    )
    return connection


def index_document(
    database_path: Path,
    user_id: str,
    document_id: str,
    *,
    embedder: Embedder | None = None,
    progress_callback: Callable[[int], object] | None = None,
    raise_on_error: bool = False,
) -> None:
    """后台构建一个文档的向量索引，并把进度写回文档元数据。"""
    store = SqliteKnowledgeDocumentStore(database_path, user_id)
    document = store.get(document_id)
    model_name = embedding_model_name()
    try:
        store.update_index_status(
            document_id, status="processing", progress=5, embedding_model=model_name
        )
        if progress_callback:
            progress_callback(5)
        from hello_agent.knowledge_documents import user_knowledge_dir

        path = user_knowledge_dir(user_id) / str(document["stored_name"])
        chunks = split_text(path.read_text(encoding="utf-8"))
        if not chunks:
            raise ValueError("文档没有可向量化的内容。")
        active_embedder = embedder or _get_embedder(database_path)
        store.update_index_status(
            document_id, status="processing", progress=15, embedding_model=model_name
        )
        if progress_callback:
            progress_callback(15)
        embeddings = list(active_embedder.embed(chunks, batch_size=16))
        if len(embeddings) != len(chunks):
            raise RuntimeError("向量模型返回的片段数量不一致。")
        store.update_index_status(
            document_id, status="processing", progress=55, embedding_model=model_name
        )
        if progress_callback:
            progress_callback(55)
        source = str(document["original_name"])
        now = datetime.now(timezone.utc).isoformat()
        with _connect(database_path) as connection:
            connection.execute(
                "DELETE FROM knowledge_vector_chunks WHERE document_id = ?",
                (document_id,),
            )
            for index, (content, embedding) in enumerate(zip(chunks, embeddings), start=1):
                connection.execute(
                    """
                    INSERT INTO knowledge_vector_chunks (
                        document_id, user_id, source, chunk_index, content,
                        embedding, embedding_model, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        user_id,
                        source,
                        index,
                        content,
                        serialize_float32(embedding),
                        model_name,
                        now,
                    ),
                )
        store.update_index_status(
            document_id, status="ready", progress=100, embedding_model=model_name
        )
        if progress_callback:
            progress_callback(100)
    except Exception as error:
        try:
            store.update_index_status(
                document_id,
                status="failed",
                progress=0,
                embedding_model=model_name,
                error_message=f"向量化失败：{error}",
            )
        except Exception:
            pass
        if progress_callback:
            try:
                progress_callback(1)
            except Exception:
                pass
        if raise_on_error:
            raise


def delete_document_vectors(database_path: Path, document_id: str) -> None:
    with _connect(database_path) as connection:
        connection.execute(
            "DELETE FROM knowledge_vector_chunks WHERE document_id = ?",
            (document_id,),
        )


def semantic_search(
    database_path: Path,
    query: str,
    owner_ids: list[str],
    limit: int = 10,
    *,
    embedder: Embedder | None = None,
) -> list[dict[str, object]]:
    if not owner_ids:
        return []
    active_embedder = embedder or _get_embedder(database_path)
    query_vectors = list(active_embedder.embed([query], batch_size=1))
    if not query_vectors:
        return []
    placeholders = ",".join("?" for _ in owner_ids)
    with _connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT source, chunk_index, content,
                   vec_distance_cosine(embedding, ?) AS distance
              FROM knowledge_vector_chunks
             WHERE user_id IN ({placeholders})
               AND embedding_model = ?
             ORDER BY distance ASC
             LIMIT ?
            """,
            [serialize_float32(query_vectors[0]), *owner_ids, embedding_model_name(), limit],
        ).fetchall()
    return [
        {
            "source": row["source"],
            "chunk": row["chunk_index"],
            "content": row["content"],
            "semantic_score": round(max(0.0, min(1.0, 1 - row["distance"])), 4),
        }
        for row in rows
    ]


def hybrid_search(
    database_path: Path,
    query: str,
    owner_ids: list[str],
    limit: int = 5,
    *,
    roots: list[Path] | None = None,
    public_root: Path | None = None,
    embedder: Embedder | None = None,
) -> dict[str, object]:
    """融合关键词与语义召回，并返回可解释分数和整体可信度。"""
    keyword = search_knowledge(query, min(10, max(limit * 2, 5)), roots, public_root)
    temporary_root = Path(tempfile.gettempdir()).resolve()
    skip_ephemeral_semantic = (
        embedder is None
        and database_path.resolve().is_relative_to(temporary_root)
        and os.getenv("KNOWLEDGE_INDEX_TEMP_DATABASES", "false").lower() != "true"
    )
    if skip_ephemeral_semantic:
        semantic = []
        semantic_error = None
    else:
        try:
            semantic = semantic_search(
                database_path, query, owner_ids, min(20, limit * 3), embedder=embedder
            )
            semantic_error = None
        except Exception as error:
            semantic = []
            semantic_error = str(error)

    merged: dict[str, dict[str, object]] = {}
    keyword_results = list(keyword["results"])
    top_keyword = max((float(item["score"]) for item in keyword_results), default=1.0)
    for item in keyword_results:
        key = _result_key(item)
        merged[key] = {
            **item,
            "keyword_score": round(float(item["score"]) / top_keyword, 4),
            "semantic_score": None,
            "retrieval": "关键词",
        }
    for item in semantic:
        key = _result_key(item)
        current = merged.get(key)
        if current is None:
            merged[key] = {
                **item,
                "keyword_score": None,
                "retrieval": "语义",
            }
        else:
            current["semantic_score"] = item["semantic_score"]
            current["retrieval"] = "混合"

    for item in merged.values():
        keyword_score = item.get("keyword_score")
        semantic_score = item.get("semantic_score")
        if keyword_score is not None and semantic_score is not None:
            score = 0.4 * float(keyword_score) + 0.6 * float(semantic_score) + 0.08
        elif semantic_score is not None:
            score = 0.8 * float(semantic_score)
        else:
            score = 0.65 * float(keyword_score or 0)
        item["score"] = round(min(1.0, score) * 100, 1)

    results = sorted(
        merged.values(),
        key=lambda item: (-float(item["score"]), str(item["source"]), int(item["chunk"])),
    )[:limit]
    top_score = float(results[0]["score"]) if results else 0.0
    confidence = "high" if top_score >= 78 else "medium" if top_score >= 55 else "low"
    message = None
    if not results or confidence == "low":
        message = "未找到足够可靠的依据，建议补充资料或换一种问法。"
    elif semantic_error:
        message = "语义检索暂不可用，本次已自动降级为关键词检索。"
    return {
        "query": query,
        "results": results,
        "document_count": keyword["document_count"],
        "confidence": confidence,
        "retrieval_mode": "hybrid" if semantic else "keyword",
        "message": message,
    }


def _result_key(item: dict[str, object]) -> str:
    content = " ".join(str(item.get("content", "")).split()).casefold()
    return content[:500]
