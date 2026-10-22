"""Harness 层：任务契约 / 确定性权限控制 / 完成门控 / 预算 / 策略。

核心叙事：Agent = Model + Harness。模型可以自由选择如何解决问题，
但权限、预算与完成证明由确定性运行时控制（docs/02 §3 ADR-D8~D10）。
"""
from app.harness.authority import DENIED_PREFIX, Decision, check, deny_reason
from app.harness.budget import BudgetTracker, get_current_budget
from app.harness.channels import (
    CHANNEL_LIMITS,
    DEFAULT_CHANNEL,
    ChannelLimits,
    graph_recursion_limit,
    heuristic_route,
    tools_for_channel,
)
from app.harness.contract import Budgets, TaskContract
from app.harness.gates import GateResult, evaluate
from app.harness.policy import (
    AllModelsFailed,
    ModelUnavailableError,
    astream_with_policy,
)
from app.harness.recovery import call_hash, execute_with_recovery, is_write_tool

__all__ = [
    "CHANNEL_LIMITS",
    "DEFAULT_CHANNEL",
    "DENIED_PREFIX",
    "AllModelsFailed",
    "BudgetTracker",
    "Budgets",
    "ChannelLimits",
    "Decision",
    "GateResult",
    "ModelUnavailableError",
    "TaskContract",
    "astream_with_policy",
    "call_hash",
    "check",
    "deny_reason",
    "evaluate",
    "execute_with_recovery",
    "get_current_budget",
    "graph_recursion_limit",
    "heuristic_route",
    "is_write_tool",
    "tools_for_channel",
]
