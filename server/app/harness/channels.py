"""通道路由（AgentHarness-通道路由改造方案）：三档预算 + 单向升级。

同一张图、三组运行参数：模型决定"怎么做"，通道决定"能用多少步/多少秒/哪些工具"。
关键约束：
- 单向升级 fast → calc → deep，无降级路径（回滚状态会污染上下文）。
- 升级只换参数不换上下文（messages 保留，escalations 只追加）。
- 通道差异收敛在节点内部，不为三个通道建三张子图。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Channel = Literal["fast", "calc", "deep"]
Risk = Literal["low", "medium", "high"]

_RISK_ORDER: dict[str, int] = {"none": -1, "low": 0, "medium": 1, "high": 2}

#: 单次工具结果过大阈值（§12）：字符数 / 结果行数任一超限即需更高通道消化。
#: 行数只认工具自报的汇总行（形如「（50 行，已截断）」），避免把 list_tables 的
#: 「orders: 1200 行」这类统计误判成结果集过大。
RESULT_MAX_CHARS = 8000
RESULT_MAX_ROWS = 50
_ROW_SUMMARY_RE = re.compile(r"（\s*(\d+)\s*行")
_TRUNCATED_MARK = "已截断"


@dataclass(frozen=True)
class ChannelLimits:
    """单通道运行参数。max_tool_risk=none 表示不挂任何工具（fast）。"""

    max_steps: int
    time_budget_s: float
    token_budget: int
    max_tool_risk: Literal["none", "low", "medium", "high"]
    allow_human_approval: bool
    escalate_to: Channel | None


CHANNEL_LIMITS: dict[str, ChannelLimits] = {
    "fast": ChannelLimits(
        max_steps=2, time_budget_s=15.0, token_budget=4_000,
        max_tool_risk="none", allow_human_approval=False, escalate_to="calc",
    ),
    "calc": ChannelLimits(
        max_steps=8, time_budget_s=60.0, token_budget=32_000,
        max_tool_risk="medium", allow_human_approval=True, escalate_to="deep",
    ),
    "deep": ChannelLimits(
        max_steps=50, time_budget_s=600.0, token_budget=128_000,
        max_tool_risk="high", allow_human_approval=True, escalate_to=None,
    ),
}

DEFAULT_CHANNEL: Channel = "deep"


def limits_of(channel: str | None) -> ChannelLimits:
    return CHANNEL_LIMITS.get(channel or DEFAULT_CHANNEL, CHANNEL_LIMITS[DEFAULT_CHANNEL])


def next_channel(channel: str) -> Channel | None:
    """下一档通道；已到 deep 返回 None。"""
    return limits_of(channel).escalate_to


def risk_allowed(channel: str, risk: str) -> bool:
    """该通道是否允许执行此风险等级的工具（只看通道，不看任务契约）。"""
    return _RISK_ORDER.get(risk, 0) <= _RISK_ORDER[limits_of(channel).max_tool_risk]


def can_escalate(channel: str, locked: bool = False) -> bool:
    return not locked and next_channel(channel) is not None


def tool_allowed(channel: str, tool_name: str, risks: dict[str, str]) -> bool:
    """按通道风险上限裁剪工具（fast 恒为空）。

    只读/检索类（low/medium）进 calc；高危写操作只在 deep 暴露，
    这样不需要在通用平台上维护场景专用的工具白名单。
    """
    limit = limits_of(channel)
    if limit.max_tool_risk == "none":
        return False
    return _RISK_ORDER.get(risks.get(tool_name, "low"), 0) <= _RISK_ORDER[limit.max_tool_risk]


def tools_for_channel(tools: list, channel: str, risks: dict[str, str], *,
                      wrap_up: bool = False, clarify: bool = False) -> list:
    """按通道/收尾状态返回本轮可绑定的工具集。"""
    if wrap_up or clarify:
        return []
    return [t for t in tools if tool_allowed(channel, t.name, risks)]


def result_size_exceeded(text: str) -> bool:
    """单次工具结果是否超过阈值（字符数 / 结果行数 / 截断标记任一命中）。

    超过阈值说明该结果需要更高通道的预算来消化：把它留在 messages 里升一级，
    而不是丢给当前通道硬扛（§6 result_too_large）。
    """
    s = str(text or "")
    if len(s) >= RESULT_MAX_CHARS or _TRUNCATED_MARK in s:
        return True
    m = _ROW_SUMMARY_RE.search(s)
    return bool(m and int(m.group(1)) > RESULT_MAX_ROWS)


def graph_recursion_limit(calc_steps: int = 8, deep_steps: int = 50, margin: int = 12) -> int:
    """recursion_limit 数的是节点访问次数，不是工具轮次。

    路径 router → guard → (agent → authority_gate → tools → guard)* → agent，
    一轮工具约占 4 跳（含 guard）；同一请求可能先走完 calc 再升 deep，按上限累计。
    """
    return 1 + (calc_steps + deep_steps) * 4 + margin


# ---- 入口判定（无模型时的确定性回退） ----

_REPORT_KW = ("报告", "交付", "图表", "汇总成", "撰写", "生成一份")
_MULTI_KW = ("分析", "为什么", "原因", "对比", "趋势", "哪些", "逐月", "连续",
             "排名", "占比", "同比", "环比", "步骤", "先看", "过程")
_TOOL_KW = ("查询", "查一下", "查查", "销售额", "订单", "统计", "列出",
            "读取", "文件", "数据库", "表", "总结", "写入", "保存", "通知", "发送")
#: 单指标查询：一次确定性取数即可作答 → fast（§3.3）
_SINGLE_METRIC_KW = ("多少", "是多少", "几个", "几条", "多少钱", "总计", "总值")

#: 缺关键参数时直接反问，不烧预算（启发式仅用于无模型/假模型场景）
_CLARIFY_INPUTS = {"查询", "查一下", "分析", "分析一下", "帮我查一下", "统计一下", "看一下"}


def heuristic_route(text: str) -> tuple[Channel, str, bool]:
    """确定性入口判定：返回 (通道, 理由, 是否需要澄清)。

    这是 router 的兜底/开发模式实现；真实模型下由 temperature=0 的结构化输出判定。
    判错的代价是慢，不是错：无法识别时按 calc 处理。
    """
    t = (text or "").strip()
    if t in _CLARIFY_INPUTS:
        return "fast", "missing_parameters", True
    if any(k in t for k in _REPORT_KW):
        return "deep", "deliverable_requires_long_chain", False
    if any(k in t for k in _MULTI_KW):
        return "calc", "multi_step_or_trial", False
    if any(k in t for k in _SINGLE_METRIC_KW):
        return "fast", "single_deterministic_query", False
    if any(k in t for k in _TOOL_KW):
        return "calc", "needs_tool_data", False
    return "fast", "single_step_answer", False
