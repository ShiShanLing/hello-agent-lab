"""通过 STDIO 提供天气查询能力的 MCP Server。"""

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from hello_agent.tools import get_weather


server = MCPServer(
    "hello-agent-weather",
    title="Hello Agent 天气服务",
    description="通过中国行政区划离线数据和 Open-Meteo 查询天气。",
    version="0.1.0",
)


@server.tool(name="get_weather")
def query_weather(
    location: Annotated[
        str,
        Field(description="中国境内的省、市、区县或地址，例如河南省驻马店市"),
    ],
    days: Annotated[
        int,
        Field(ge=1, le=7, description="需要预报的天数，默认 3"),
    ] = 3,
) -> dict[str, object]:
    """查询中国省、市、区县的实时天气和未来 1 到 7 天预报。"""
    return {"weather": get_weather(location, days)}


def main() -> None:
    """使用标准输入输出启动 MCP 服务。"""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
