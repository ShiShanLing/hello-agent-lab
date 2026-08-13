"""为已有知识文档补建或重建向量索引。"""

import os
from pathlib import Path
import sqlite3

from hello_agent.app import DEFAULT_DATABASE_FILE
from hello_agent.vector_knowledge import index_document


def main() -> None:
    database_path = Path(
        os.getenv("TODO_DATABASE_FILE", str(DEFAULT_DATABASE_FILE))
    ).resolve()
    with sqlite3.connect(database_path) as connection:
        documents = connection.execute(
            "SELECT id, user_id, original_name FROM knowledge_documents "
            "ORDER BY created_at"
        ).fetchall()
    total = len(documents)
    for position, (document_id, user_id, original_name) in enumerate(documents, start=1):
        print(f"[{position}/{total}] 正在向量化 {original_name}", flush=True)
        index_document(database_path, user_id, document_id)
    print(f"向量索引完成，共处理 {total} 份文档。", flush=True)


if __name__ == "__main__":
    main()
