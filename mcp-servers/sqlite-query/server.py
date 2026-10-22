"""内置 MCP Server：sqlite-query（stdio, 只读）。

工具：
- list_tables()            列出表与行数
- query(sql)               仅允许 SELECT 的只读查询

环境变量：
- SQLITE_DB_PATH  sqlite 数据库绝对路径（首次启动自动建演示表 orders）
"""
from __future__ import annotations

import os
import sqlite3

from mcp.server.mcpserver import MCPServer

DB_PATH = os.environ.get("SQLITE_DB_PATH", "demo.db")

mcp = MCPServer(name="sqlite-query", instructions="只读 SQLite 查询（演示数据）")


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _seed() -> None:
    """首次运行建演示表：orders(id, customer, amount, status, created_at)。"""
    with _conn() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                   id INTEGER PRIMARY KEY,
                   customer TEXT NOT NULL,
                   amount REAL NOT NULL,
                   status TEXT NOT NULL,
                   created_at TEXT NOT NULL)"""
        )
        if db.execute("SELECT count(*) FROM orders").fetchone()[0] == 0:
            rows = [
                (1, "张三", 128.5, "paid", "2026-10-07"),
                (2, "李四", 89.0, "paid", "2026-10-07"),
                (3, "王五", 245.0, "pending", "2026-10-07"),
                (4, "赵六", 59.9, "paid", "2026-10-06"),
                (5, "张三", 310.0, "refunded", "2026-10-05"),
                (6, "李四", 76.2, "paid", "2026-10-07"),
                (7, "小明", 199.0, "paid", "2026-10-08"),
            ]
            db.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", rows)


@mcp.tool(description="列出数据库中的所有表及行数")
def list_tables() -> str:
    with _conn() as db:
        names = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        if not names:
            return "（空数据库）"
        out = []
        for n in names:
            c = db.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0]
            out.append(f"{n}: {c} 行")
        return "\n".join(out)


@mcp.tool(description="执行只读 SQL 查询（仅允许 SELECT），返回制表符分隔的结果文本")
def query(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if not s.lower().startswith("select"):
        return "ERROR: 只读 Server，仅允许 SELECT 查询"
    if ";" in s:
        return "ERROR: 不允许多条语句"
    try:
        with _conn() as db:
            cur = db.execute(s)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(200)
    except Exception as e:
        return f"ERROR: SQL 执行失败: {e}"
    lines = ["\t".join(cols)]
    lines += ["\t".join(str(x) for x in r) for r in rows]
    if len(rows) == 200:
        lines.append("…（结果截断至 200 行）")
    return "\n".join(lines)


if __name__ == "__main__":
    _seed()
    mcp.run()
