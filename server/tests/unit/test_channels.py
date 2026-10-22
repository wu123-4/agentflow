"""通道路由单元测试（AgentHarness-通道路由改造方案 §4/§8）。

不启动服务：纯函数层面的参数表、单向升级、风险裁剪、启发式判定与 recursion_limit。
"""
from __future__ import annotations

from app.harness.channels import (
    CHANNEL_LIMITS,
    DEFAULT_CHANNEL,
    can_escalate,
    graph_recursion_limit,
    heuristic_route,
    limits_of,
    next_channel,
    result_size_exceeded,
    risk_allowed,
    tools_for_channel,
)


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def test_channel_limits_match_plan():
    assert (CHANNEL_LIMITS["fast"].max_steps, CHANNEL_LIMITS["fast"].time_budget_s,
            CHANNEL_LIMITS["fast"].token_budget) == (2, 15.0, 4_000)
    assert (CHANNEL_LIMITS["calc"].max_steps, CHANNEL_LIMITS["calc"].time_budget_s,
            CHANNEL_LIMITS["calc"].token_budget) == (8, 60.0, 32_000)
    assert (CHANNEL_LIMITS["deep"].max_steps, CHANNEL_LIMITS["deep"].time_budget_s,
            CHANNEL_LIMITS["deep"].token_budget) == (50, 600.0, 128_000)
    assert not CHANNEL_LIMITS["fast"].allow_human_approval
    assert CHANNEL_LIMITS["calc"].allow_human_approval


def test_escalation_is_one_way():
    assert next_channel("fast") == "calc"
    assert next_channel("calc") == "deep"
    assert next_channel("deep") is None
    assert can_escalate("fast") and can_escalate("calc")
    assert not can_escalate("deep")
    # 锁定通道（显式指定 / 组装型任务）不升级
    assert not can_escalate("calc", locked=True)


def test_default_channel_is_deep():
    assert DEFAULT_CHANNEL == "deep"
    assert limits_of(None) is CHANNEL_LIMITS["deep"]
    assert limits_of("unknown") is CHANNEL_LIMITS["deep"]


def test_risk_ceiling_per_channel():
    assert not risk_allowed("fast", "low")       # fast 不挂任何工具
    assert risk_allowed("calc", "low")
    assert risk_allowed("calc", "medium")
    assert not risk_allowed("calc", "high")      # 高危写只在 deep
    assert risk_allowed("deep", "high")


def test_tools_for_channel_filters_by_risk_and_state():
    tools = [_Tool("low_tool"), _Tool("mid_tool"), _Tool("high_tool")]
    risks = {"low_tool": "low", "mid_tool": "medium", "high_tool": "high"}
    assert tools_for_channel(tools, "fast", risks) == []
    assert [t.name for t in tools_for_channel(tools, "calc", risks)] == \
        ["low_tool", "mid_tool"]
    assert [t.name for t in tools_for_channel(tools, "deep", risks)] == \
        ["low_tool", "mid_tool", "high_tool"]
    # 收尾 / 澄清：清空工具集
    assert tools_for_channel(tools, "deep", risks, wrap_up=True) == []
    assert tools_for_channel(tools, "deep", risks, clarify=True) == []


def test_heuristic_route_three_channels():
    cases = {
        "你好": "fast",
        "读取 readme 文件": "calc",
        # 单指标查询：一次确定性取数即可作答 → fast（§3.3）
        "上个月华东区销售额多少": "fast",
        "哪些品类连续三个月下滑": "calc",
        "生成一份 Q3 经营分析报告": "deep",
        "随便聊聊": "fast",
    }
    for text, want in cases.items():
        channel, reason, clarify = heuristic_route(text)
        assert channel == want, (text, channel, reason)
        assert not clarify


def test_heuristic_route_clarify_on_missing_params():
    channel, reason, clarify = heuristic_route("查询")
    assert clarify and reason == "missing_parameters"
    assert channel == "fast"


def test_recursion_limit_covers_full_budget():
    # 一轮工具 4 跳（agent→gate→tools→guard），recursion_limit 必须覆盖 calc+deep 步数
    assert graph_recursion_limit() > (CHANNEL_LIMITS["calc"].max_steps
                                      + CHANNEL_LIMITS["deep"].max_steps) * 4


def test_result_size_exceeded_thresholds():
    # 正常小结果不升级
    assert not result_size_exceeded("query_id: q_1a2b3c4d\nmonth\tsales\n2026-07\t123\n（3 行）")
    # 字符数超限
    assert result_size_exceeded("x" * 8_000)
    # 结果行数超限 / 被截断（run_sql 自报的汇总行）
    assert result_size_exceeded("col\n（80 行）")
    assert result_size_exceeded("col\n（50 行，已截断）")
    # list_tables 的统计行不是结果集，不应误判
    assert not result_size_exceeded("orders: 1200 行\ncustomers: 120 行")
