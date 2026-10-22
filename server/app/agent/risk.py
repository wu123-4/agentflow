"""工具风险分级（docs/04 §3.5）：全局动词规则 + Server 级 risk_overrides 覆盖。

M2 只做"判定与元数据"：风险等级随工具信息暴露给管理 API 与前端展示；
M3 的 hitl_gate 将基于 high 等级强制 HITL 确认。
"""
from __future__ import annotations

# 动词 -> 风险等级（命中即返回，按列表顺序优先）
_RULES: list[tuple[str, list[str]]] = [
    ("high", ["delete", "remove", "drop", "truncate", "send", "notify", "publish",
              "insert", "update", "write_file", "execute"]),
    ("medium", ["create", "edit", "move", "rename", "mkdir", "upload", "set"]),
    ("low", ["read", "list", "search", "query", "fetch", "get", "describe", "stat",
             "preview", "count"]),
]


def classify(tool_name: str, overrides: dict[str, str] | None = None) -> str:
    """返回 low | medium | high。overrides 以 MCP 原始工具名（不含前缀）为键。"""
    if overrides and tool_name in overrides:
        return overrides[tool_name]
    n = tool_name.lower()
    for level, verbs in _RULES:
        for v in verbs:
            if v in n:
                return level
    return "low"
