"""通道路由评估集（AgentHarness-通道路由改造方案 §11）。

覆盖：分档（fast/calc/deep）/ clarify 不烧工具 / 连续拒绝升级 / 连续工具失败升级 /
deep 与锁定通道的 wrap_up / 报告引用真实 query_id / 非法通道 422。
"""
from __future__ import annotations

import re


def _selected(evs) -> dict:
    items = [d for e, d in evs if e == "channel_selected"]
    return items[0] if items else {}


def _ok_tool_names(evs) -> set[str]:
    """tool_end 成功事件只带 call_id，工具名需从 tool_start 映射。"""
    names = {d.get("call_id"): d.get("tool") for d in
             (d for e, d in evs if e == "tool_start")}
    return {names.get(d.get("call_id")) for d in
            (d for e, d in evs if e == "tool_end") if d.get("status") == "ok"}


def test_routing_fast_single_step(client):
    """单步问题 → fast，无工具，无升级。"""
    sid = client.mk_session("routing fast")
    evs = client.chat("你好，请介绍一下你自己", session_id=sid)
    fin = client.score("routing:fast", evs, expect="end_turn")
    assert _selected(evs).get("channel") == "fast", _selected(evs)
    assert fin.get("channel") == "fast", fin
    assert fin.get("escalations") == []
    assert not client.events(evs, "tool_start"), "fast 通道不应挂工具"


def test_routing_calc_multi_step(client):
    """多步分析 → calc，直接执行只读 SQL，无升级。"""
    sid = client.mk_session("routing calc")
    evs = client.chat("哪些品类连续三个月下滑，原因是什么", session_id=sid)
    fin = client.score("routing:calc", evs, expect="end_turn")
    assert _selected(evs).get("channel") == "calc", _selected(evs)
    assert fin.get("channel") == "calc" and fin.get("escalations") == [], fin
    oks = _ok_tool_names(evs)
    assert "nl2sql__run_sql" in oks, oks


def test_routing_fast_single_query(client):
    """单指标查询 → fast：程序执行一次确定性取数，不进工具循环、不升级。"""
    sid = client.mk_session("routing fast query")
    evs = client.chat("上个月华东区销售额多少", session_id=sid)
    fin = client.score("routing:fast_query", evs, expect="end_turn")
    assert _selected(evs).get("channel") == "fast", _selected(evs)
    assert fin.get("channel") == "fast" and fin.get("escalations") == [], fin
    ends = [d for d in client.tool_ends(evs) if d.get("status") in ("ok", "error")]
    assert len(ends) == 1, ends  # 恰好一次取数
    assert _ok_tool_names(evs) == {"nl2sql__run_sql"}, _ok_tool_names(evs)
    assert client.text_of(evs).strip(), "fast 也要产出答复"


def test_routing_deep_deliverable_cites_query_id(client):
    """交付物 → deep；报告必须引用真实 query_id，门控放行。"""
    sid = client.mk_session("routing deep")
    evs = client.chat("生成一份 Q3 经营分析报告", session_id=sid,
                      contract={"invariants": ["report_cites_queries"]})
    fin = client.score("routing:deep", evs, expect="end_turn")
    assert _selected(evs).get("channel") == "deep", _selected(evs)
    assert fin.get("channel") == "deep", fin
    oks = _ok_tool_names(evs)
    assert {"nl2sql__run_sql", "nl2sql__write_report"} <= oks, oks
    assert (fin.get("gate_result") or {}).get("passed") is True, fin
    assert re.search(r"q_[0-9a-f]{8}", client.text_of(evs)), "最终答复应引用 query_id"


def test_routing_clarify_does_not_call_tools(client):
    """缺关键参数 → clarify，零工具调用。"""
    sid = client.mk_session("routing clarify")
    evs = client.chat("查询", session_id=sid)
    fin = client.score("routing:clarify", evs, expect="end_turn")
    assert _selected(evs).get("decision") == "clarify", _selected(evs)
    assert not client.events(evs, "tool_start")
    assert client.text_of(evs).strip(), "澄清应产出反问文本"
    assert fin.get("escalations") == []


def test_routing_deny_twice_escalates(client):
    """AuthorityController 连续 DENY 2 次 → calc 升 deep（原因 guard_denied_twice）。"""
    sid = client.mk_session("routing deny")
    evs = client.chat("重试拒绝 读取 readme 文件", session_id=sid,
                      contract={"denied_tools": ["filesystem__read_text_file"]})
    fin = client.score("routing:deny_twice", evs, expect="end_turn")
    esc = fin.get("escalations") or []
    assert len(esc) == 1, esc
    assert esc[0]["from"] == "calc" and esc[0]["to"] == "deep", esc
    assert esc[0]["reason"] == "guard_denied_twice", esc
    assert fin.get("channel") == "deep", fin
    assert any(d.get("status") == "denied" for d in client.tool_ends(evs))


def test_routing_tool_failure_twice_escalates(client):
    """工具连续失败 2 次 → 只升一级（tool_failed_twice），不无限升级。"""
    sid = client.mk_session("routing tool fail")
    evs = client.chat("SQL错误 查询", session_id=sid,
                      contract={"gates_enabled": False})
    fin = client.score("routing:tool_fail", evs, expect="end_turn")
    esc = fin.get("escalations") or []
    assert len(esc) == 1, esc
    assert esc[0]["from"] == "calc" and esc[0]["to"] == "deep", esc
    assert esc[0]["reason"] == "tool_failed_twice", esc
    assert fin.get("channel") == "deep", fin


def test_routing_long_task_escalates_then_wraps_up(client):
    """步数耗尽逐级升级（fast→calc→deep），deep 到顶 wrap_up 且有答复。"""
    sid = client.mk_session("routing long")
    evs = client.chat("长任务", session_id=sid,
                      contract={"budgets": {"max_iterations": 120}})
    fin = client.score("routing:long_wrap", evs, expect="end_turn")
    esc = fin.get("escalations") or []
    assert [e["from"] for e in esc] == ["fast", "calc"], esc
    assert [e["to"] for e in esc] == ["calc", "deep"], esc
    assert fin.get("channel") == "deep" and fin.get("wrap_up") is True, fin
    assert client.text_of(evs).strip(), "收尾必须产出答复"
    assert any(d.get("reason") == "steps_exhausted"
               for d in client.events(evs, "budget_exhausted"))


def test_routing_locked_channel_wraps_up_without_escalation(client):
    """显式指定通道即锁定：步数用尽直接收尾，不升级。"""
    sid = client.mk_session("routing locked")
    evs = client.chat("长任务", session_id=sid, channel="calc")
    fin = client.score("routing:locked", evs, expect="end_turn")
    assert fin.get("channel") == "calc", fin
    assert fin.get("escalations") == [], fin
    assert fin.get("wrap_up") is True, fin
    assert client.text_of(evs).strip(), "收尾必须产出答复"
    assert not client.events(evs, "escalated")


def test_routing_invalid_channel_422(client):
    sid = client.mk_session("routing 422")
    code = client.post_status("/chat", {"session_id": sid, "content": "你好",
                                        "channel": "turbo"})
    client.record(case="routing:invalid_channel", success=(code == 422))
    assert code == 422


def test_routing_model_requested_escalates(client):
    """模型显式调用 request_deep → calc 升 deep（原因 model_requested）。"""
    sid = client.mk_session("routing model requested")
    evs = client.chat("深入分析 申请升级", session_id=sid)
    fin = client.score("routing:model_requested", evs, expect="end_turn")
    esc = fin.get("escalations") or []
    assert len(esc) == 1, esc
    assert esc[0]["from"] == "calc" and esc[0]["to"] == "deep", esc
    assert esc[0]["reason"] == "model_requested", esc
    assert fin.get("channel") == "deep", fin


def test_routing_result_too_large_escalates(client):
    """单次工具结果过大（>8000 字符）→ 升一级，结果随上下文带走（result_too_large）。"""
    sid = client.mk_session("routing big result")
    evs = client.chat("查询 大结果", session_id=sid)
    fin = client.score("routing:result_too_large", evs, expect="end_turn")
    esc = fin.get("escalations") or []
    assert len(esc) == 1, esc
    assert esc[0]["from"] == "calc" and esc[0]["to"] == "deep", esc
    assert esc[0]["reason"] == "result_too_large", esc
    assert fin.get("channel") == "deep", fin


def test_routing_gate_evidence_insufficient_escalates(client):
    """门控判定"证据不足"时先升档再判：fast→calc→deep 逐级升，deep 到顶才 gate_blocked。"""
    sid = client.mk_session("routing gate evidence")
    evs = client.chat("你好", session_id=sid,
                      contract={"invariants": ["requires_tool:nl2sql__run_sql"]})
    fin = client.score("routing:gate_evidence", evs, expect="gate_blocked")
    esc = fin.get("escalations") or []
    assert [e["from"] for e in esc] == ["fast", "calc"], esc
    assert [e["to"] for e in esc] == ["calc", "deep"], esc
    assert [e["reason"] for e in esc] == ["gate_insufficient_evidence"] * 2, esc
    assert fin.get("channel") == "deep", fin
    assert fin.get("stop_reason") == "gate_blocked", fin
