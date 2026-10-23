"""NL2SQL 只读守卫（方案 §11 #14）：越权 SQL 在执行前被拒绝，原因可回喂模型改写。"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("NL2SQL_DB_PATH", str(tmp_path / "nl2sql.db"))
    monkeypatch.setenv("NL2SQL_REPORT_DIR", str(tmp_path / "reports"))
    path = Path(__file__).resolve().parents[3] / "mcp-servers" / "nl2sql" / "server.py"
    spec = importlib.util.spec_from_file_location("nl2sql_server_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guard_allows_select_and_appends_limit(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    sql, err = m._guard("SELECT * FROM orders")
    assert err is None
    assert sql.endswith("LIMIT 50")


def test_guard_rejects_write_and_ddl(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    for bad in ("DELETE FROM orders", "DROP TABLE orders",
                "UPDATE orders SET id = 1"):
        sql, err = m._guard(bad)
        assert sql == "" and err, bad


def test_guard_rejects_multi_statement(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    _, err = m._guard("SELECT * FROM orders; DROP TABLE orders")
    assert err is not None


def test_guard_rejects_table_outside_whitelist(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    _, err = m._guard("SELECT * FROM secret_users")
    assert err and "白名单" in err


def test_guard_rejects_oversized_limit(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    _, err = m._guard("SELECT * FROM orders LIMIT 500")
    assert err and "LIMIT" in err


def test_join_grain_warns_when_both_keys_non_unique(tmp_path, monkeypatch):
    """两侧关联键都不唯一 -> 告警（语法对但行数会被放大）。"""
    m = _load(tmp_path, monkeypatch)
    warns = m._join_grain_warnings(
        "SELECT * FROM orders o JOIN customers c ON o.region_id = c.region_id")
    assert warns and "放大" in warns[0]


def test_join_grain_silent_on_unique_key(tmp_path, monkeypatch):
    """一侧是唯一键（customers.id）-> 不告警。"""
    m = _load(tmp_path, monkeypatch)
    assert m._join_grain_warnings(
        "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id") == []


def test_run_sql_reuses_result_for_same_sql(tmp_path, monkeypatch):
    """同一条 SQL（仅空白差异）第二次命中缓存，返回同一 query_id。"""
    m = _load(tmp_path, monkeypatch)
    first = m.run_sql("SELECT count(*) AS n FROM orders")
    second = m.run_sql("SELECT  count(*)  AS n  FROM orders")
    assert m._QUERY_ID.search(first).group(0) == m._QUERY_ID.search(second).group(0)
    assert "复用" in second


def test_write_report_resolves_placeholders(tmp_path, monkeypatch):
    """报告里的数字由程序按 query_id + 行号 + 列名取值。"""
    m = _load(tmp_path, monkeypatch)
    out = m.run_sql("SELECT count(*) AS n FROM orders")
    qid = m._QUERY_ID.search(out).group(0)
    res = m.write_report("订单数", f"订单数：{{{{{qid}.r1.n}}}}")
    assert res.startswith("报告已写入")
    path = Path(res.split("报告已写入 ")[1].split("（")[0])
    body = path.read_text(encoding="utf-8")
    assert "{{" not in body and qid not in body


def test_write_report_rejects_unresolved_placeholder(tmp_path, monkeypatch):
    """引用了不存在的 query_id -> 拒绝写报告。"""
    m = _load(tmp_path, monkeypatch)
    res = m.write_report("t", "订单数：{{q_deadbeef.r1.n}}")
    assert res.startswith("ERROR") and "无法解析" in res
