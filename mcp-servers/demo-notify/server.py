"""内置 MCP Server：demo-notify（stdio, 高风险演示用）。

工具：
- send_message(recipient, message)  模拟外发通知（写入日志文件）。
  按风险规则命中 high，M3 起将强制 HITL 确认。

环境变量：
- NOTIFY_LOG_PATH  通知日志文件绝对路径
"""
from __future__ import annotations

import os
from datetime import datetime

from mcp.server.mcpserver import MCPServer

LOG_PATH = os.environ.get("NOTIFY_LOG_PATH", "notifications.log")

mcp = MCPServer(name="demo-notify", instructions="演示外发通知（高风险）")


@mcp.tool(description="向指定收件人发送一条通知消息（模拟外发，高风险操作）")
def send_message(recipient: str, message: str) -> str:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] -> {recipient}: {message}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        return f"ERROR: 写入通知日志失败: {e}"
    return f"已发送通知给 {recipient}（共 {len(message)} 字，已记录到日志）"


if __name__ == "__main__":
    mcp.run()
