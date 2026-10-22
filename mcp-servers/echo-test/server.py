"""测试用 MCP Server：echo-test（stdio）。仅用于 M2 热插拔验收。

工具：
- echo(text)  原样返回文本
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="echo-test")


@mcp.tool(description="回显输入文本（测试热插拔）")
def echo(text: str) -> str:
    return f"ECHO: {text}"


if __name__ == "__main__":
    mcp.run()
