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
