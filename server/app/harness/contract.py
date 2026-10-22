"""Task Contract（任务契约）：Agent 启动前显式定义产出、范围与预算。

契约是"可执行信封"——模型在信封内自由探索，运行时强制其不越界。
字段经 pydantic 校验（API 层非法契约直接 422），JSON Schema 可导出。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Budgets(BaseModel):
    """资源预算：None 表示该项不限（回退全局配置）。"""

    max_tokens: int | None = None        # 本次 run 的 token 总预算
    max_tool_calls: int | None = None    # 工具调用次数上限
    max_iterations: int | None = None    # agent 迭代轮数上限（None -> settings.max_iterations）


class TaskContract(BaseModel):
    """任务契约（docs/01 FR-25~28）。

    - max_risk: 允许的最高工具风险等级（low/medium/high），超出即 DENY
    - allowed_tools / denied_tools: 工具白/黑名单（限定名 {server}__{tool}）
    - gates_enabled: 完成门控开关（输出非空 / 工具成功率 / 预算校验）
    - invariants: 预留，Phase 3 消费
    """

    outcome: str | None = None
    max_risk: Literal["low", "medium", "high"] = "high"
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    budgets: Budgets = Field(default_factory=Budgets)
    gates_enabled: bool = True
    invariants: list[str] = Field(default_factory=list)

    @classmethod
    def default(cls) -> TaskContract:
        """默认契约：不收紧风险、不开预算限制，行为与改造前一致（回归兼容）。"""
        return cls()

    def summary(self) -> dict:
        """SSE run_started 中的契约回显（不含敏感内容）。"""
        return {
            "max_risk": self.max_risk,
            "budgets": self.budgets.model_dump(),
            "gates_enabled": self.gates_enabled,
        }
