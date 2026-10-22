"""内置 MCP Server：nl2sql（stdio, 只读语义 + 交付物）。

NL2SQL 分析助手的最小场景（AgentHarness-通道路由改造方案 §3）：
- 数据源为**合成**电商订单数据集（SQLite 单文件，首次启动自动建表并灌数），
  不接任何公司数据、表结构或客户信息。
- 5 个工具：list_tables / describe_table / run_sql / render_chart / write_report。
- run_sql 只允许单条 SELECT，且必须命中表级白名单；无 LIMIT 时程序补 LIMIT，
  超出结果上限直接拒绝。每次成功查询返回 `query_id`（q_xxxxxxxx），
  报告类交付必须引用它，由 CompletionGate 的 report_cites_queries 校验出处。

环境变量：
- NL2SQL_DB_PATH     sqlite 数据库绝对路径（默认 nl2sql.db）
- NL2SQL_REPORT_DIR  报告输出目录（默认 reports）
"""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import uuid
from datetime import date, timedelta
from pathlib import Path

from mcp.server.mcpserver import MCPServer

DB_PATH = os.environ.get("NL2SQL_DB_PATH", "nl2sql.db")
REPORT_DIR = os.environ.get("NL2SQL_REPORT_DIR", "reports")

#: 允许查询的表（行级/表级权限的最小实现：白名单外一律拒绝）
ALLOWED_TABLES = {"regions", "products", "customers", "orders", "order_items"}

#: 结果集上限（防止大表全扫描撑爆上下文）
MAX_ROWS = 50
MAX_LIMIT = 200
MAX_CHARS = 8000

#: 只读守卫：任何写/DDL/管理语句一律拒绝
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|"
    r"vacuum|reindex|trigger|grant|revoke)\b", re.I)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
_LIMIT = re.compile(r"\blimit\s+(\d+)", re.I)
_QUERY_ID = re.compile(r"q_[0-9a-f]{8}")

mcp = MCPServer(name="nl2sql", instructions="只读电商订单数据查询与报告交付（合成数据）")


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _seed() -> None:
    """首次运行建 5 张表并灌入确定性的合成数据（random.Random(42)）。"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _conn() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS regions (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL,
                region_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,
                region_id INTEGER NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL, quantity INTEGER NOT NULL,
                unit_price REAL NOT NULL);
            """
        )
        if db.execute("SELECT count(*) FROM orders").fetchone()[0]:
            return
        rng = random.Random(42)
        regions = [(1, "华东"), (2, "华北"), (3, "华南"), (4, "西南"), (5, "华中")]
        products = [
            (1, "轻薄本", "数码"), (2, "无线耳机", "数码"), (3, "智能手表", "数码"),
            (4, "空气净化器", "家电"), (5, "扫地机器人", "家电"), (6, "电饭煲", "家电"),
            (7, "冲锋衣", "服饰"), (8, "运动鞋", "服饰"), (9, "坚果礼盒", "食品"),
            (10, "咖啡豆", "食品"),
        ]
        customers = [
            (i, f"客户{i:03d}", rng.choice(["上海", "北京", "广州", "成都", "武汉"]),
             rng.randint(1, 5))
            for i in range(1, 121)
        ]
        db.executemany("INSERT INTO regions VALUES (?,?)", regions)
        db.executemany("INSERT INTO products VALUES (?,?,?)", products)
        db.executemany("INSERT INTO customers VALUES (?,?,?,?)", customers)
        # 2026 Q3：7/1 ~ 9/30，每日若干订单
        start = date(2026, 7, 1)
        order_id = item_id = 0
        orders, items = [], []
        for day in range(92):
            d = start + timedelta(days=day)
            for _ in range(rng.randint(8, 18)):
                order_id += 1
                cust = rng.choice(customers)
                status = rng.choices(["paid", "refunded", "pending"], [0.86, 0.08, 0.06])[0]
                orders.append((order_id, cust[0], cust[3], status, d.isoformat()))
                for _ in range(rng.randint(1, 3)):
                    item_id += 1
                    pid = rng.randint(1, 10)
                    qty = rng.randint(1, 4)
                    base = {1: 4999.0, 2: 699.0, 3: 1299.0, 4: 1899.0, 5: 2599.0,
                            6: 399.0, 7: 899.0, 8: 599.0, 9: 129.0, 10: 89.0}[pid]
                    items.append((item_id, order_id, pid, qty,
                                  round(base * rng.uniform(0.85, 1.05), 2)))
        db.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
        db.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)


def _guard(sql: str) -> tuple[str, str | None]:
    """只读守卫：返回 (可执行 SQL, 错误信息)。

    规则（任一不满足即拒绝，错误文本回喂模型改写）：
    - 单条 SELECT（不允许分号拼接、不允许 CTE 之外的写语句）
    - 表必须在白名单内
    - 无 LIMIT 时程序补 MAX_ROWS；显式 LIMIT 不得超过 MAX_LIMIT
    """
    s = (sql or "").strip()
    if not s:
        return "", "SQL 为空"
    s = s.rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", s):
        return "", "只允许只读查询（必须以 SELECT 或 WITH 开头）"
    if ";" in s:
        return "", "不允许多条语句"
    if _FORBIDDEN.search(s):
        return "", "检测到写/DDL/管理语句，已拒绝执行"
    tables = {t.lower() for t in _TABLE_REF.findall(s)}
    unknown = tables - ALLOWED_TABLES
    if unknown:
        return "", f"表不在白名单内：{', '.join(sorted(unknown))}"
    m = _LIMIT.search(s)
    if m:
        if int(m.group(1)) > MAX_LIMIT:
            return "", f"LIMIT 不得超过 {MAX_LIMIT}（大结果集会撑爆上下文）"
    else:
        s = f"{s} LIMIT {MAX_ROWS}"
    return s, None


@mcp.tool(description="列出可查询的数据表与行数")
def list_tables() -> str:
    with _conn() as db:
        out = []
        for t in sorted(ALLOWED_TABLES):
            c = db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            out.append(f"{t}: {c} 行")
    return "\n".join(out)


@mcp.tool(description="查看表结构与字段含义，参数为表名")
def describe_table(table: str) -> str:
    t = (table or "").strip().lower()
    if t not in ALLOWED_TABLES:
        return f"ERROR: 表不在白名单内：{table}"
    with _conn() as db:
        cols = db.execute(f'PRAGMA table_info("{t}")').fetchall()
    return "\n".join(f"{row[1]} {row[2]}" for row in cols)


@mcp.tool(description=(
    "执行只读 SQL（仅单条 SELECT），返回 query_id、列名与结果行。表结构："
    "regions(id,name) products(id,name,category) customers(id,name,city,region_id) "
    "orders(id,customer_id,region_id,status,created_at) "
    "order_items(id,order_id,product_id,quantity,unit_price)；金额=quantity*unit_price"))
def run_sql(sql: str) -> str:
    safe, err = _guard(sql)
    if err:
        return f"ERROR: {err}"
    try:
        with _conn() as db:
            cur = db.execute(safe)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(MAX_ROWS + 1)
    except Exception as e:  # noqa: BLE001 — 业务性失败包成 ERROR 回喂模型
        return f"ERROR: SQL 执行失败: {e}"
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    qid = f"q_{uuid.uuid4().hex[:8]}"
    lines = [f"query_id: {qid}", "\t".join(cols)]
    lines += ["\t".join("" if v is None else str(v) for v in r) for r in rows]
    lines.append(f"（{len(rows)} 行{'，已截断' if truncated else ''}）")
    text = "\n".join(lines)
    return text[:MAX_CHARS]


@mcp.tool(description="根据查询结果生成图表描述（JSON）：title、kind(bar|line|pie)、data_json")
def render_chart(title: str, kind: str = "bar", data_json: str = "[]") -> str:
    k = (kind or "bar").strip().lower()
    if k not in ("bar", "line", "pie"):
        return "ERROR: kind 仅支持 bar|line|pie"
    try:
        data = json.loads(data_json or "[]")
    except json.JSONDecodeError as e:
        return f"ERROR: data_json 不是合法 JSON: {e}"
    spec = {"title": title, "kind": k, "data": data}
    return json.dumps(spec, ensure_ascii=False)


@mcp.tool(description="写入 Markdown 报告文件，必须引用 run_sql 返回的 query_id")
def write_report(title: str, markdown: str) -> str:
    if not (markdown or "").strip():
        return "ERROR: 报告内容为空"
    if not _QUERY_ID.search(markdown):
        return "ERROR: 报告未引用任何 query_id，请先 run_sql 取数并在报告中标注出处"
    out_dir = Path(REPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", (title or "report"))[:40]
    path = out_dir / f"{slug}_{uuid.uuid4().hex[:6]}.md"
    body = markdown if markdown.lstrip().startswith("#") else f"# {title}\n\n{markdown}"
    path.write_text(body, encoding="utf-8")
    return f"报告已写入 {path}（{len(body)} 字符，引用 {len(set(_QUERY_ID.findall(markdown)))} 个查询结果）"


if __name__ == "__main__":
    _seed()
    mcp.run()
else:
    _seed()
