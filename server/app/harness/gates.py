"""CompletionGate（完成门控，docs/01 FR-27 + Harness Phase 3 invariants）。

不让模型从"一段有说服力的摘要"中宣布成功：任务定义时预设门控，
运行结束时由确定性代码评估——全部通过才标记 end_turn，否则 gate_blocked。

契约 invariants 为最小字符串谓词 DSL（未知谓词按 fail-closed 拦截）：
- no_denied               本轮没有被 Harness 拒绝的调用
- denied<=N               拒绝次数不超过 N
- requires_tool:<name>    至少一次成功调用过工具 <name>（限定名或后缀匹配）
- tool_success_rate>=X    工具成功率阈值（0~1）
- report_cites_queries    最终答复至少引用一个真实出现过在工具输出中的 query_id

query_id 形如 `q_1a2b3c4d`（NL2SQL 的 run_sql 每次执行返回）。
报告类交付必须可溯源：引用了不存在的 query_id 或没有引用，都判为证据不足。
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.harness.contract import TaskContract

#: 工具调用成功率阈值（有工具调用时才校验）
_TOOL_SUCCESS_THRESHOLD = 0.9

#: NL2SQL 查询结果 ID（run_sql 返回，用于报告溯源）
_QUERY_ID_RE = re.compile(r"q_[0-9a-f]{8}")


class GateResult(BaseModel):
    passed: bool
    blocked_by: str | None = None
    checked: list[dict] = Field(default_factory=list)  # [{name, passed}]


def _check_invariant(expr: str, *, denied_total: int, tool_total: int,
                     tool_failed: int, succeeded_tools: list[str],
                     output: str = "", tool_outputs: list[str] | None = None,
                     deliverable: str = "") -> bool:
    """校验单条 invariant 谓词；无法解析的谓词返回 False（fail-closed）。"""
    e = expr.strip()
    if e == "no_denied":
        return denied_total == 0
    m = re.fullmatch(r"denied\s*<=\s*(\d+)", e)
    if m:
        return denied_total <= int(m.group(1))
    m = re.fullmatch(r"requires_tool\s*:\s*(\S+)", e)
    if m:
        want = m.group(1)
        return any(t == want or t.endswith(f"__{want}") or want.endswith(f"__{t}")
                   for t in succeeded_tools)
    m = re.fullmatch(r"tool_success_rate\s*>=\s*([0-9.]+)", e)
    if m:
        if tool_total == 0:
            return False
        return (tool_total - tool_failed) / tool_total >= float(m.group(1))
    if e == "report_cites_queries":
        # 交付物（报告正文）或答复必须引用真实查询结果：
        # cited 与工具输出中的 query_id 有交集；只在交付物里编造出处仍会被拦截
        cited = set(_QUERY_ID_RE.findall((output or "") + "\n" + (deliverable or "")))
        known: set[str] = set()
        for t in (tool_outputs or []):
            known.update(_QUERY_ID_RE.findall(str(t)))
        return bool(cited & known)
    return False


def evaluate(*, output: str, tool_total: int, tool_failed: int,
             tokens_used: int, contract: TaskContract,
             denied_total: int = 0,
             succeeded_tools: list[str] | None = None,
             tool_outputs: list[str] | None = None,
             deliverable: str = "") -> GateResult:
    """对一次自然结束（end_turn）的 run 做完成门控评估。

    - output_non_empty:  最终答复非空（空输出视为未完成任务）
    - tool_success_rate: 工具调用成功率 >= 0.9（有调用时才校验）
    - token_budget:      token 消耗未超契约预算（配置了预算才校验）
    - tool_call_budget:  工具调用次数未超契约预算（配置了预算才校验）
    - invariant:<expr>:  契约任务不变量（Harness Phase 3，逐条校验）

    deliverable：交付物正文（如 write_report 的 markdown），供 report_cites_queries
    校验"报告是否引用真实 query_id"——报告是交付物，出处不必重复写在对话答复里。
    """
    gates: list[tuple[str, bool]] = [("output_non_empty", bool(output.strip()))]
    if tool_total > 0:
        gates.append(("tool_success_rate",
                      (tool_total - tool_failed) / tool_total >= _TOOL_SUCCESS_THRESHOLD))
    b = contract.budgets
    if b.max_tokens is not None:
        gates.append(("token_budget", tokens_used <= b.max_tokens))
    if b.max_tool_calls is not None:
        gates.append(("tool_call_budget", tool_total <= b.max_tool_calls))
    for expr in contract.invariants:
        gates.append((f"invariant:{expr}",
                      _check_invariant(expr, denied_total=denied_total,
                                       tool_total=tool_total, tool_failed=tool_failed,
                                       succeeded_tools=succeeded_tools or [],
                                       output=output,
                                       tool_outputs=tool_outputs,
                                       deliverable=deliverable)))
    blocked_by = next((name for name, passed in gates if not passed), None)
    return GateResult(passed=blocked_by is None, blocked_by=blocked_by,
                      checked=[{"name": n, "passed": p} for n, p in gates])
