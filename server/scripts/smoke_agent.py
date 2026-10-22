"""冒烟测试：FakeChatModel + 真实 MCP filesystem 工具 + MemorySaver，
验证 LangGraph ReAct 循环与 SSE 事件转译管道（不需要 LLM API Key）。

运行：uv run python scripts/smoke_agent.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

import app.agent.graph as graph_mod
from app.agent.graph import build_graph
from app.agent.mcp_bridge import MCPServerConnection, to_langchain_tools
from app.config import MCP_FILESYSTEM_ABS_DIR
from langgraph.checkpoint.memory import MemorySaver


async def main() -> None:
    conn = MCPServerConnection(
        server_id="filesystem", command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", MCP_FILESYSTEM_ABS_DIR],
    )
    await conn.start()
    try:
        tools = to_langchain_tools(conn, await conn.list_tools())
        print(f"工具数: {len(tools)}")

        # 脚本：第一轮返回 read_text_file 工具调用，第二轮返回文本总结
        fake = FakeMessagesListChatModel(responses=[
            AIMessage(content="", tool_calls=[{
                "id": "call_1", "name": "filesystem__read_text_file",
                "args": {"path": str(Path(MCP_FILESYSTEM_ABS_DIR) / "readme.txt")},
            }]),
            AIMessage(content="已读取文件，内容是 AgentFlow 演示工作区。"),
        ])
        # 让 build_model 返回可 bind_tools 的 fake（FakeChatModel 原生不支持 bind_tools）
        class _FakeWithTools:
            def bind_tools(self, tools):  # noqa: ARG002
                return fake
        graph_mod.build_model = lambda model_id=None: _FakeWithTools()

        g = build_graph(tools, MemorySaver())
        events = []
        async for ev in g.astream_events(
            {"messages": [HumanMessage(content="读取 readme.txt 并总结")], "tool_calls_count": 0},
            {"configurable": {"thread_id": "t1"}}, version="v2",
        ):
            events.append(ev["event"])
            if ev["event"] in ("on_tool_start", "on_tool_end", "on_chain_end"):
                print(ev["event"], "→", str(ev.get("data"))[:200])

        assert "on_tool_start" in events, "未触发工具调用"
        assert "on_tool_end" in events, "工具未返回结果"
        final = await g.ainvoke(
            {"messages": [HumanMessage(content="读取 readme.txt 并总结")], "tool_calls_count": 0},
            {"configurable": {"thread_id": "t2"}})
        assert "演示工作区" in final["messages"][-1].content, "最终答复错误"
        print("SMOKE OK: ReAct 循环 + MCP 工具调用 + 事件流 全部通过")
    finally:
        await conn.stop()


asyncio.run(main())
