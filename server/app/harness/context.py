"""Harness Phase 3：上下文治理（熵管理，docs/07 Phase 3）。

长任务不劣化：估算上下文占用超过阈值时，由确定性代码压缩/外化历史，
而非等模型"自己注意"。两种策略（settings.context_strategy）：

- compaction：把早期消息折叠为一条固定 id 的摘要 SystemMessage（RemoveMessage 收缩
  state，随 checkpoint 持久化），保留最近 context_keep_recent 条。
- reset：把"目标 + 关键事实 + 最近摘要"外化写入工作区 context_state_path 文件，
  历史同样收缩，后续轮次注入"状态已外化到文件"提示。

摘要为模板式（不调用模型），保证验收确定性。触发条件：
估算 token（字符数/4）>= context_compact_ratio * context_window，或消息数 >= context_max_messages。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES  # noqa: F401  （备用全清语义）

from app.config import MCP_FILESYSTEM_ABS_DIR, settings

SUMMARY_ID = "agentflow-context-summary"

#: 摘要消息标记名（避免被后续压缩再次折叠进摘要）
_SUMMARY_TAG = "agentflow-context-summary"


def _text_of(m: BaseMessage) -> str:
    c = m.content
    return c if isinstance(c, str) else str(c)


def estimate_tokens(messages: list[BaseMessage]) -> int:
    """粗略估算 token（字符数/4，中英混合场景足够触发治理）。"""
    total = 0
    for m in messages:
        total += len(_text_of(m))
        total += len(str(getattr(m, "tool_calls", None) or ""))
    return total // 4


def _summarize(dropped: list[BaseMessage]) -> str:
    """模板式历史摘要：用户意图 + 工具调用清单。"""
    user_lines: list[str] = []
    tool_lines: list[str] = []
    for m in dropped:
        role = getattr(m, "type", "")
        if role == "human":
            user_lines.append(_text_of(m)[:80])
        elif role == "system" and getattr(m, "name", None) == _SUMMARY_TAG:
            # 反复压缩时保留"用户请求要点"：长任务跨多次治理仍不丢失目标
            for line in _text_of(m).splitlines():
                if line.startswith("用户请求要点:"):
                    user_lines.append(line.split(":", 1)[1].strip())
        elif role == "ai":
            for c in (getattr(m, "tool_calls", None) or []):
                tool_lines.append(c.get("name", "?"))
        elif role == "tool":
            tool_lines.append("（工具结果已折叠）")
    parts = ["【历史摘要（Harness 自动压缩）】"]
    if user_lines:
        parts.append("用户请求要点: " + " | ".join(dict.fromkeys(user_lines)))
    if tool_lines:
        parts.append("已执行工具: " + ", ".join(dict.fromkeys(tool_lines)))
    parts.append("以上历史已压缩，最近消息保持完整。")
    return "\n".join(parts)


def _tail_start_ok(messages: list[BaseMessage], keep: int) -> int:
    """返回保留尾部的起始下标；若尾部以 ToolMessage 开头，向前扩展以带上其 AIMessage。"""
    idx = max(0, len(messages) - keep)
    while idx > 0 and getattr(messages[idx], "type", "") == "tool":
        idx -= 1
    return idx


def maybe_govern(messages: list[BaseMessage], *, context_window: int
                 ) -> tuple[list[BaseMessage], str] | None:
    """需要治理时返回 (更新列表, 策略名)，否则 None。

    更新列表可直接作为 agent 节点对 state["messages"] 的返回值
    （add_messages reducer 会应用 RemoveMessage）。
    """
    est = estimate_tokens(messages)
    over_tokens = context_window > 0 and est >= settings.context_compact_ratio * context_window
    over_count = len(messages) >= settings.context_max_messages
    if not (over_tokens or over_count):
        return None

    strategy = settings.context_strategy
    if strategy == "auto":
        strategy = "compaction"
    if strategy not in ("compaction", "reset"):
        strategy = "compaction"

    # 保留首条系统提示（非摘要标记）
    head = 1 if (messages and getattr(messages[0], "type", "") == "system"
                 and getattr(messages[0], "name", None) != _SUMMARY_TAG) else 0
    tail_idx = _tail_start_ok(messages, settings.context_keep_recent)
    dropped = messages[head:tail_idx]
    if len(dropped) < 2:  # 可压缩的太少，不值得治理
        return None

    summary = SystemMessage(content=_summarize(dropped), id=SUMMARY_ID, name=_SUMMARY_TAG)
    if strategy == "reset":
        state_file = Path(MCP_FILESYSTEM_ABS_DIR) / settings.context_state_path
        try:
            state_file.write_text(
                "# AgentFlow 上下文状态（Harness Reset 外化）\n\n"
                + _summarize(dropped) + "\n", encoding="utf-8")
            summary.content += (f"\n完整状态已外化到文件：{state_file}"
                                "（如需细节可用 filesystem 工具读取该文件）。")
        except OSError:
            pass  # 外化失败不影响压缩本身
    return [RemoveMessage(m.id) for m in dropped] + [summary], strategy
