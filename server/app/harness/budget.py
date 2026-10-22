"""per-run 预算记账（Harness Phase 2）。

线程安全（目标：单实例并发会话 ≥ 50）；用 contextvar 隔离并发 run，
使图节点（agent）能读取当前 run 的剩余预算并注入推理上下文（主动收敛）。

不抛异常：超限由 runtime 干净终止（docs/02 ADR-D10）。
"""
from __future__ import annotations

import threading
from contextvars import ContextVar

from app.harness.contract import Budgets


class BudgetTracker:
    """单次 run 的 token 记账。"""

    def __init__(self, run_id: str, budgets: Budgets, tokens_used: int = 0) -> None:
        self.run_id = run_id
        self.budgets = budgets
        self._tokens = int(tokens_used or 0)
        self._lock = threading.Lock()

    def record_tokens(self, input_tokens: int, output_tokens: int) -> int:
        """累加 token 用量，返回累计值。"""
        with self._lock:
            self._tokens += int(input_tokens or 0) + int(output_tokens or 0)
            return self._tokens

    @property
    def tokens_used(self) -> int:
        with self._lock:
            return self._tokens

    def tokens_remaining(self) -> int | None:
        """剩余 token 预算；未设置 max_tokens 时返回 None（不限）。"""
        cap = self.budgets.max_tokens
        if cap is None:
            return None
        return max(0, cap - self.tokens_used)

    def token_exhausted(self) -> bool:
        cap = self.budgets.max_tokens
        return cap is not None and self.tokens_used > cap

    def snapshot(self, tool_calls_used: int | None = None) -> dict:
        """预算快照（供注入上下文与 run_finished 事件）。"""
        cap_calls = self.budgets.max_tool_calls
        return {
            "run_id": self.run_id,
            "tokens_used": self.tokens_used,
            "tokens_remaining": self.tokens_remaining(),
            "max_tokens": self.budgets.max_tokens,
            "tool_calls_used": tool_calls_used,
            "tool_calls_remaining": (
                max(0, cap_calls - tool_calls_used)
                if cap_calls is not None and tool_calls_used is not None
                else None
            ),
            "max_tool_calls": cap_calls,
            "max_iterations": self.budgets.max_iterations,
        }


current_budget: ContextVar[BudgetTracker | None] = ContextVar("current_budget", default=None)


def get_current_budget() -> BudgetTracker | None:
    return current_budget.get()
