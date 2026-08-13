"""向 Agent 提供关键词 + 语义向量混合检索能力的 MCP Server。"""

import os
from pathlib import Path

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from hello_agent.knowledge import list_knowledge_files, search_knowledge
from hello_agent.vector_knowledge import hybrid_search


server = MCPServer(
    "hello-agent-knowledge",
    title="Hello Agent 本地知识库",
    description="检索 knowledge 目录中的 Markdown 和 TXT 学习资料。",
    version="0.1.0",
)


@server.tool(name="search_knowledge")
def search_documents(
    query: Annotated[str, Field(description="需要在本地资料中搜索的问题或关键词")],
    limit: Annotated[int, Field(ge=1, le=10, description="最多返回多少个片段")] = 5,
) -> dict[str, object]:
    """混合搜索本地知识库，返回引用来源、命中分数和可信度。"""
    database_file = os.getenv("TODO_DATABASE_FILE", "").strip()
    owner_ids = [
        value
        for value in os.getenv("KNOWLEDGE_OWNER_IDS", "").split(os.pathsep)
        if value
    ]
    if database_file and owner_ids:
        return hybrid_search(
            Path(database_file),
            query,
            owner_ids,
            limit,
        )
    result = search_knowledge(query, limit)
    result.update(confidence="low", retrieval_mode="keyword")
    return result


@server.tool(name="list_knowledge_files")
def list_documents() -> dict[str, object]:
    """列出当前本地知识库中可搜索的 Markdown 和 TXT 文件。"""
    files = list_knowledge_files()
    return {"files": files, "count": len(files)}


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
