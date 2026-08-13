import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hello_agent.database import SqliteKnowledgeDocumentStore
from hello_agent.knowledge_documents import save_knowledge_document
from hello_agent.vector_knowledge import hybrid_search, index_document


class FakeChineseEmbedder:
    def embed(self, documents, **_kwargs):
        for text in documents:
            if any(word in text for word in ("退款", "售后", "退钱")):
                yield [1.0, 0.0, 0.0]
            elif any(word in text for word in ("天气", "气温")):
                yield [0.0, 1.0, 0.0]
            else:
                yield [0.0, 0.0, 1.0]


class VectorKnowledgeTest(unittest.TestCase):
    def test_indexes_document_and_finds_semantic_paraphrase(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "agent.db"
            upload_root = root / "uploads"
            old_upload = os.environ.get("KNOWLEDGE_UPLOAD_DIR")
            os.environ["KNOWLEDGE_UPLOAD_DIR"] = str(upload_root)
            try:
                store = SqliteKnowledgeDocumentStore(database, "user-1")
                document = save_knowledge_document(
                    store,
                    "user-1",
                    "售后政策.md",
                    "商品签收七天内可以申请退款。".encode(),
                    "产品资料",
                )
                index_document(
                    database,
                    "user-1",
                    str(document["id"]),
                    embedder=FakeChineseEmbedder(),
                )

                indexed = store.get(str(document["id"]))
                self.assertEqual(indexed["status"], "ready")
                self.assertEqual(indexed["embedding_progress"], 100)

                result = hybrid_search(
                    database,
                    "买完东西怎样退钱",
                    ["user-1"],
                    roots=[upload_root / "user-1"],
                    limit=3,
                    embedder=FakeChineseEmbedder(),
                )
                self.assertEqual(result["retrieval_mode"], "hybrid")
                self.assertEqual(result["confidence"], "high")
                self.assertEqual(result["results"][0]["source"], "售后政策.md")
                self.assertGreater(result["results"][0]["semantic_score"], 0.99)
            finally:
                if old_upload is None:
                    os.environ.pop("KNOWLEDGE_UPLOAD_DIR", None)
                else:
                    os.environ["KNOWLEDGE_UPLOAD_DIR"] = old_upload


if __name__ == "__main__":
    unittest.main()
