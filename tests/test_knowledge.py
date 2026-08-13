"""本地 Markdown/TXT 知识库测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hello_agent.knowledge import list_knowledge_files, search_knowledge


class KnowledgeSearchTest(unittest.TestCase):
    def test_searches_markdown_and_returns_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.md").write_text(
                "# MCP\n\nMCP Client 负责连接服务，MCP Server 负责公开工具。",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"KNOWLEDGE_DIR": directory}):
                result = search_knowledge("MCP Server", limit=3)

        self.assertEqual(result["results"][0]["source"], "agent.md")
        self.assertIn("公开工具", result["results"][0]["content"])

    def test_lists_only_supported_safe_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes.txt").write_text("学习 Agent", encoding="utf-8")
            (root / "ignored.json").write_text("{}", encoding="utf-8")
            with patch.dict("os.environ", {"KNOWLEDGE_DIR": directory}):
                files = list_knowledge_files()

        self.assertEqual([file["source"] for file in files], ["notes.txt"])

    def test_reports_when_no_relevant_content_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "notes.md").write_text("Python 函数基础", encoding="utf-8")
            with patch.dict("os.environ", {"KNOWLEDGE_DIR": directory}):
                result = search_knowledge("量子物理")

        self.assertEqual(result["results"], [])
        self.assertIn("没有找到", result["message"])


if __name__ == "__main__":
    unittest.main()
