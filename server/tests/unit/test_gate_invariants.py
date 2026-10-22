"""完成门控 invariants 单元测试：报告溯源与 fail-closed。"""
from __future__ import annotations

from app.harness.contract import TaskContract
from app.harness.gates import evaluate

_QUERY_OUTPUT = "query_id: q_1a2b3c4d\nmonth\tsales\n2026-07\t123\n（3 行）"


def _eval(output: str, *, tool_outputs=None, invariants=None):
    return evaluate(output=output, tool_total=1, tool_failed=0, tokens_used=10,
                    contract=TaskContract(invariants=invariants
                                          or ["report_cites_queries"]),
                    tool_outputs=tool_outputs or [_QUERY_OUTPUT])


def test_report_without_citation_is_blocked():
    r = _eval("本季度销售额稳步增长，无异常。")
    assert not r.passed
    assert r.blocked_by == "invariant:report_cites_queries"


def test_report_with_real_query_id_passes():
    r = _eval("数据来源：q_1a2b3c4d，Q3 逐月销售额见上表。")
    assert r.passed, r


def test_report_with_fabricated_query_id_is_blocked():
    r = _eval("数据来源：q_deadbeef（模型编造的出处）")
    assert not r.passed
    assert r.blocked_by == "invariant:report_cites_queries"


def test_unknown_invariant_fails_closed():
    r = _eval("任何输出", invariants=["made_up_predicate"])
    assert not r.passed
    assert r.blocked_by == "invariant:made_up_predicate"


def test_report_citation_in_deliverable_passes():
    """出处写在交付物（报告正文）里也应放行：报告是交付物，不必在答复里重复。"""
    r = evaluate(output="报告已生成，见 reports/xxx.md。", tool_total=1, tool_failed=0,
                 tokens_used=10, contract=TaskContract(invariants=["report_cites_queries"]),
                 tool_outputs=[_QUERY_OUTPUT],
                 deliverable="# 报告\n数据来源：q_1a2b3c4d")
    assert r.passed, r


def test_report_fabricated_citation_in_deliverable_is_blocked():
    """交付物里引用不存在的 query_id 同样拦截（防编造数字）。"""
    r = evaluate(output="报告已生成。", tool_total=1, tool_failed=0, tokens_used=10,
                 contract=TaskContract(invariants=["report_cites_queries"]),
                 tool_outputs=[_QUERY_OUTPUT],
                 deliverable="# 报告\n数据来源：q_deadbeef")
    assert not r.passed
    assert r.blocked_by == "invariant:report_cites_queries"
