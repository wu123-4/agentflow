"""开发启动入口（Windows 下 psycopg 异步与 Proactor 事件循环冲突，
runtime 已自动改用同步 PostgresSaver，因此这里保持默认 Proactor 循环即可，
MCP stdio 子进程需要 Proactor）。

用法：uv run python run.py
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
