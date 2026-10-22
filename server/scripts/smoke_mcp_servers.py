"""冒烟测试内置 MCP Server（sqlite-query / demo-notify）。
用法: uv run python scripts/smoke_mcp_servers.py
"""
import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]


async def run_case(name: str, script: str, env: dict, calls: list[tuple[str, dict]]):
    p = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp-servers" / script)],
        env=env,
    )
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print(f"[{name}] tools = {names}")
            for tool, args in calls:
                res = await s.call_tool(tool, args)
                text = res.content[0].text if res.content else ""
                print(f"  {tool}({args}) -> {text[:120]!r}")


async def main() -> None:
    await run_case(
        "sqlite-query", "sqlite-query/server.py",
        {"SQLITE_DB_PATH": str(ROOT / "workspace" / "demo.db")},
        [("list_tables", {}),
         ("query", {"sql": "SELECT count(*) FROM orders"}),
         ("query", {"sql": "DELETE FROM orders"}),
         ("query", {"sql": "SELECT customer, amount FROM orders WHERE status='paid'"})],
    )
    await run_case(
        "demo-notify", "demo-notify/server.py",
        {"NOTIFY_LOG_PATH": str(ROOT / "workspace" / "notifications.log")},
        [("send_message", {"recipient": "ops", "message": "部署完成"})],
    )
    print("SMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
