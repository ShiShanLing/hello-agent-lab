"""天气 MCP Server 与客户端适配器的协议测试。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hello_agent.mcp_client import (
    KnowledgeMCPClient,
    MCPClientRegistry,
    WeatherMCPClient,
)


class WeatherMCPClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = WeatherMCPClient()

    def test_discovers_weather_tool_over_stdio(self) -> None:
        weather_tool = self.client.tools[0]["function"]

        self.assertEqual(weather_tool["name"], "get_weather")
        self.assertIn("location", weather_tool["parameters"]["required"])
        self.assertEqual(
            weather_tool["parameters"]["properties"]["days"]["maximum"],
            7,
        )

    def test_returns_mcp_validation_error_as_tool_result(self) -> None:
        result = json.loads(
            self.client.call_tool(
                "get_weather",
                '{"location":"上海","days":8}',
            )
        )

        self.assertIn("error", result)
        self.assertIn("less than or equal to 7", result["error"])


class KnowledgeMCPClientTest(unittest.TestCase):
    def test_discovers_two_knowledge_tools(self) -> None:
        names = KnowledgeMCPClient().tool_names

        self.assertEqual(
            names,
            {"search_knowledge", "list_knowledge_files"},
        )

    def test_searches_file_through_real_stdio_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "memory.md").write_text(
                "RAG 会先检索资料片段，再交给大语言模型生成回答。",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"KNOWLEDGE_DIR": directory}):
                result = json.loads(
                    KnowledgeMCPClient().call_tool(
                        "search_knowledge",
                        '{"query":"什么是 RAG","limit":3}',
                    )
                )

        self.assertEqual(result["results"][0]["source"], "memory.md")

    def test_registry_combines_multiple_mcp_servers(self) -> None:
        registry = MCPClientRegistry()

        self.assertIn("get_weather", registry.tool_names)
        self.assertIn("search_knowledge", registry.tool_names)
        self.assertEqual(len(registry.server_infos()), 2)


if __name__ == "__main__":
    unittest.main()
