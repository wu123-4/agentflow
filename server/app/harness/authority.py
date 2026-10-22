"""AuthorityController（确定性权限控制，docs/01 FR-26）。

模型可以决定"做什么"，但契约和代码决定"能不能做"：
- DENY          超出任务契约（黑名单/白名单外/风险超限），不执行、拒绝消息回喂模型
- REQUIRE_HITL  契约允许但高风险，走 interrupt 人工确认（M3 链路）
- ALLOW         直接执行
"""
from __future__ import annotations

from enum import Enum

from app.harness.contract import TaskContract

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

#: 拒绝 ToolMessage 的统一前缀（runtime 据此识别并下发 denied tool_end 事件）
DENIED_PREFIX = "操作被 Harness 拒绝"


class Decision(str, Enum):
    ALLOW = "allow"
    REQUIRE_HITL = "require_hitl"
    DENY = "deny"


def check(tool_name: str, risk: str, contract: TaskContract) -> Decision:
    """对一次工具调用申请做权限裁决（纯函数，无副作用）。"""
    if contract.denied_tools and tool_name in contract.denied_tools:
        return Decision.DENY
    if contract.allowed_tools is not None and tool_name not in contract.allowed_tools:
        return Decision.DENY
    if _RISK_ORDER.get(risk, 0) > _RISK_ORDER[contract.max_risk]:
        return Decision.DENY
    if risk == "high":
        return Decision.REQUIRE_HITL
    return Decision.ALLOW


def deny_reason(tool_name: str, risk: str, contract: TaskContract) -> str | None:
    """DENY 时的人类可读原因（check 返回非 DENY 时为 None）。"""
    if contract.denied_tools and tool_name in contract.denied_tools:
        return f"工具 {tool_name} 在任务契约黑名单中"
    if contract.allowed_tools is not None and tool_name not in contract.allowed_tools:
        return f"工具 {tool_name} 不在任务契约白名单中"
    if _RISK_ORDER.get(risk, 0) > _RISK_ORDER[contract.max_risk]:
        return f"风险等级 {risk} 超出契约允许上限（max_risk={contract.max_risk}）"
    return None
