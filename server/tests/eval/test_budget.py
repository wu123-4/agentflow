"""Phase 4 评估集 · 预算（5）：token 超限干净终止 / 预算快照一致性 / 工具预算 / gate 预算校验 / 迭代上限。"""
from __future__ import annotations


def test_budget_token_exceeded(client):
    sid = client.mk_session("eval bud token")
    evs = client.chat("你好", session_id=sid, model="deepseek-chat",
                      contract={"budgets": {"max_tokens": 10}})
    fin = client.score("budget:token_exceeded", evs, expect="budget_exceeded")
    assert fin.get("stop_reason") == "budget_exceeded", fin
    assert any(d.get("code") == "budget_exceeded" for d in client.events(evs, "error"))
    assert (fin.get("usage") or {}).get("input_tokens", 0) > 10


def test_budget_snapshot_consistency(client):
    sid = client.mk_session("eval bud snap")
    evs = client.chat("你好", session_id=sid,
                      contract={"budgets": {"max_tokens": 100000}})
    fin = client.score("budget:snapshot", evs, expect="end_turn")
    b = fin.get("budget") or {}
    u = fin.get("usage") or {}
    used = u.get("input_tokens", 0) + u.get("output_tokens", 0)
    assert b.get("tokens_used") == used and b.get("max_tokens") == 100000, b
    assert b.get("tokens_remaining") == 100000 - used, b


def test_budget_tool_calls(client):
    """max_tool_calls=1：首次读文件 ALLOW 执行，第二次写被预算 DENY。"""
    sid = client.mk_session("eval bud tools")
    evs = client.chat("读取 readme 文件", session_id=sid,
                      contract={"budgets": {"max_tool_calls": 1}})
    fin = client.score("budget:tool_calls", evs, expect="end_turn")
    assert fin.get("stop_reason") == "end_turn", fin
    b = fin.get("budget") or {}
    assert b.get("max_tool_calls") == 1 and (b.get("tool_calls_used") or 0) <= 1, b


def test_budget_gate_check(client):
    """gate 预算校验：token 超契约预算 → gate_blocked(token_budget)。

    预算极小但大于单轮用量（假模型每轮 40+~n token），让任务先自然结束，
    再由门控核对预算。max_tokens=45：单轮问候 40+1=41 通过不了 tool 校验，
    用读文件两轮任务使累计必超。
    """
    sid = client.mk_session("eval bud gate")
    evs = client.chat("读取 readme 文件", session_id=sid,
                      contract={"budgets": {"max_tokens": 45}})
    fin = client.score("budget:gate", evs, expect=("budget_exceeded", "gate_blocked"))
    # 两种合法终态：流中途 budget_exceeded（>45 即终止）或自然结束被门控拦截
    sr = fin.get("stop_reason")
    assert sr in ("budget_exceeded", "gate_blocked"), fin
    if sr == "gate_blocked":
        assert (fin.get("gate_result") or {}).get("blocked_by") == "token_budget"


def test_budget_iterations(client):
    """迭代上限：max_iterations=1 时工具循环被限制在 1 次内强制收敛。"""
    sid = client.mk_session("eval bud iter")
    evs = client.chat("读取 readme 文件", session_id=sid,
                      contract={"budgets": {"max_iterations": 1}})
    fin = client.score("budget:iterations", evs, expect="end_turn")
    assert fin.get("stop_reason") == "end_turn", fin
    assert len(client.events(evs, "tool_start")) <= 1
