"""LangGraph ReAct 图（Harness Phase 1）：agent -> authority_gate -> (wait_approval) -> tools -> agent -> END。

- agent 节点：绑定 MCP 工具调用模型，产出文本或 tool_calls
- authority_gate 节点（原 hitl_gate 扩展）：Harness 确定性权限控制——
  对照 Task Contract 与风险分级，DENY 记入 denied_calls（不执行、拒绝消息回喂），
  契约内 high 风险生成 pending_approval 走 HITL；并强制 tool_calls 预算
- wait_approval 节点：interrupt() 暂停（由 Checkpointer 持久化），
  消费 Command(resume=...)：批准/改参 -> tools；拒绝 -> 生成拒绝 ToolMessage 回喂 agent
- tools 节点：执行 tool_calls（含改参覆盖），denied_calls 产出拒绝 ToolMessage
- 条件边：有 tool_calls 走门控，否则结束；迭代超限（契约预算优先）强制结束

契约经 per-run state 注入（task_contract 字段随 checkpoint 持久化，中断恢复后仍生效），
不 build 时固化——图缓存按 model 复用，契约变更零成本（docs/02 ADR-D8）。
"""
from __future__ import annotations

import json
import re
import time
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import Field

from app.harness import (
    DENIED_PREFIX,
    TaskContract,
    astream_with_policy,
    authority,
    heuristic_route,
    tools_for_channel,
)
from app.harness.channels import (
    can_escalate,
    limits_of,
    next_channel,
    result_size_exceeded,
    risk_allowed,
)
from app.harness.context import maybe_govern
from app.harness.gates import evaluate as evaluate_gates
from app.harness.policy import ModelUnavailableError
from app.harness.recovery import call_hash, execute_with_recovery


def uuid4hex() -> str:
    return uuid4().hex[:12]


# 假模型瞬态故障去重：同一 nonce 首次失败、重试成功（使 tenacity 重试可确定性验收）
_RETRY_NONCE_RE = re.compile(r"(?:重试|限流)\s*([0-9a-fA-F]{4,32})")
_seen_nonces: set[str] = set()

DEFAULT_SYSTEM = (
    "你是 AgentFlow 智能体。你可以调用工具完成任务。"
    "调用工具前简要说明你要做什么；完成后用中文给出清晰、简洁的答复。"
    "注意：filesystem 工具的路径参数必须使用绝对路径。"
)

#: 模型显式申请升级的合成工具名（§6 model_requested）。
#: 它不是 MCP 工具，只在 calc 通道临时绑定；模型一旦调用即升 deep，不执行任何副作用。
REQUEST_DEEP_NAME = "request_deep"


class RequestDeepTool(BaseTool):
    """calc 通道专用：模型判断任务需要交付物/长链路时，显式申请升级到 deep。"""

    name: str = REQUEST_DEEP_NAME
    description: str = (
        "当本任务需要生成交付物（报告/图表）或长链路分析、当前通道预算不足时调用，"
        "申请升级到 deep 通道。调用后不立即产出结果。"
    )

    def _run(self, **kwargs: Any) -> str:
        return "已申请升级到 deep 通道。"

    async def _arun(self, **kwargs: Any) -> str:
        return "已申请升级到 deep 通道。"


REQUEST_DEEP_TOOL = RequestDeepTool()

#: 假模型取数用的固定只读 SQL（router 的 fast 动作与 calc 取数分支共用，确定性可断言）
_FAKE_SALES_SQL = ("SELECT r.name AS region, "
                   "ROUND(SUM(oi.quantity * oi.unit_price), 2) AS sales "
                   "FROM orders o "
                   "JOIN regions r ON r.id = o.region_id "
                   "JOIN order_items oi ON oi.order_id = o.id "
                   "GROUP BY r.name ORDER BY sales DESC")

#: 假模型判定「需要一次取数」的关键词（与 heuristic_route 的单指标/取数意图对齐）
_FAKE_METRIC_KW = ("销售额", "订单", "统计", "排名", "趋势", "品类", "多少")


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    tool_calls_count: int
    pending_approval: dict | None   # {call_id, tool, args, risk, reason}
    approval_result: dict | None    # resume 值 {decision, edited_args, reason}
    task_contract: dict | None      # per-run 任务契约（随 checkpoint 持久化）
    denied_calls: list              # authority_gate 裁决的拒绝 [{call_id, tool, reason}]
    model_fallbacks: dict | None    # Harness Phase 2：累计 {model_used, fallbacks:[{from,to,error}]}
    recent_call_hashes: list        # Harness Phase 3：最近执行的 (tool,args) 哈希（循环检测）
    loop_detected: bool             # Harness Phase 3：检测到连续重复调用
    context_events: list            # Harness Phase 3：上下文治理事件（runtime 转 SSE）

    # ---- 通道路由（AgentHarness-通道路由改造方案） ----
    requested_channel: str | None   # 请求显式指定通道（含则锁定，不再升级）
    channel: str                    # 当前通道 fast|calc|deep
    channel_locked: bool            # 锁定后不再升级（显式指定/组装型任务）
    step_count: int                 # 模型轮次计数（不是节点访问次数）
    started_at: float               # time.monotonic()，时间预算起点
    escalations: list               # [{from,to,reason,at_step,elapsed_s}] 只追加
    route_decision: dict | None     # 入口判定结果 {decision, channel, reason, clarify}
    clarify: bool                   # 缺关键参数：直接反问，不挂工具
    wrap_up: bool                   # 到顶收尾：清空工具，再要一次纯文字作答
    escalate_pending: bool          # 本轮需要升级（由节点置位，路由消费）
    escalate_reason: str | None
    deny_streak: int                # 连续被 Harness 拒绝的轮数
    tool_fail_streak: int           # 连续工具失败次数
    budget_events: list             # 通道预算耗尽/收尾事件（runtime 转 SSE）


def _loop_threshold() -> int:
    from app.config import settings

    return settings.loop_detect_threshold


def _contract_of(state: AgentState) -> TaskContract:
    raw = state.get("task_contract")
    return TaskContract.model_validate(raw) if raw else TaskContract.default()


#: router 节点的固定标记（DevFakeChatModel 据此返回确定性判定）
ROUTER_MARKER = "任务路由器"
ROUTER_PROMPT = (
    f"你是{ROUTER_MARKER}。判断任务所需预算档位，并在需要时给出「一次确定性取数」的具体动作。"
    "只输出一行 JSON：\n"
    '{"channel":"fast|calc|deep","reason":"简短理由","clarify":false,'
    '"action":{"tool":"工具限定名","args":{}}|null}\n'
    "- fast: 单步可答，或只需一次确定性取数（单指标查询、单条只读查询）\n"
    "- calc: 需要多次取数、试错、对比或聚合\n"
    "- deep: 需要交付物（报告/图表）或长链路分析\n"
    "- 仅当 channel=fast 且确实需要一次取数时填 action（工具须为只读/检索类），否则填 null\n"
    "- 若缺少关键参数无法判断对象，clarify=true\n"
    "不要输出 JSON 以外的内容。"
)


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        raise ValueError("no json")
    return json.loads(m.group(0))


#: 判定「一次确定性取数」时 router 需要知道的工具类型（只取只读/取数类）
_ROUTER_TOOL_KW = ("sql", "查询", "数据", "表", "table", "query")


def _tools_hint(tools: list[BaseTool], risks: dict[str, str]) -> str:
    """给 router 的低风险取数工具说明。

    fast 的 action 由结构化输出给出，router 需要知道有哪些只读取数工具、以及它们的
    字段口径（写在工具描述里）才能写出可执行的一次取数；这里只"告知"，**不绑定**
    工具 schema，因此不触发工具循环（§2）。
    """
    items: list[str] = []
    for t in tools:
        if risks.get(t.name, "low") != "low":
            continue
        desc = " ".join((t.description or "").split())
        low = f"{t.name} {desc}".lower()
        if not any(k in low for k in _ROUTER_TOOL_KW):
            continue
        items.append(f"- {t.name}: {desc[:220]}")
    return "\n".join(items)


async def route_decision(model: Any, text: str, tools_hint: str = "") -> dict:
    """入口判定：结构化输出（temperature=0，不挂工具）。

    解析失败一律按 calc 处理：判错的代价是慢，不是错。
    action 只在 fast 且确需一次确定性取数时给出（§5.4 direct），由程序执行，不走工具循环。
    """
    system = ROUTER_PROMPT
    if tools_hint:
        system += f"\n可用的只读取数工具（供填写 action 参考）：\n{tools_hint}"
    try:
        msgs = [SystemMessage(content=system), HumanMessage(content=text)]
        res = await model.bind(temperature=0).ainvoke(msgs)
        data = _extract_json(str(res.content))
        channel = data.get("channel")
        reason = str(data.get("reason") or "model_decision")
        clarify = bool(data.get("clarify"))
        raw_action = data.get("action")
        action = raw_action if isinstance(raw_action, dict) else None
    except Exception:  # noqa: BLE001 — 路由失败不影响主流程，按 calc 兜底
        channel, reason, clarify, action = "calc", "fallback_error", False, None
    if channel not in ("fast", "calc", "deep"):
        channel, reason, clarify, action = "calc", "fallback_unparsed", False, None
    if channel != "fast" or clarify:
        action = None  # 只有 fast 才走「一次确定性取数」
    return {"decision": "clarify" if clarify else ("direct" if channel == "fast" else "escalate"),
            "channel": channel, "reason": reason, "clarify": clarify, "action": action}


def _channel_hint(state: AgentState) -> str | None:
    """把当前通道与剩余预算注入推理上下文，让模型主动收敛（瞬态，不落 state）。"""
    channel = state.get("channel") or "deep"
    limit = limits_of(channel)
    steps = state.get("step_count", 0)
    parts = [(
        f"当前通道 {channel}：最多 {limit.max_steps} 轮、"
        f"{int(limit.time_budget_s)} 秒、约 {limit.token_budget} token"
    )]
    if limit.max_tool_risk == "none":
        parts.append("本通道不提供任何工具，禁止输出任何工具调用标记（XML/DSML 等），"
                     "只能基于已有信息用中文直接作答")
    parts.append(f"已用 {steps} 轮")
    return "【通道提示】" + "；".join(parts) + "。超出预算会自动升级或收尾。"


async def _recall_memory(state: AgentState) -> str:
    """Harness Phase 4：长期记忆召回（开关关闭/异常时静默返回空串）。"""
    from app.config import settings

    if not settings.memory_enabled:
        return ""
    try:
        from app.services.memory import recall_prompt

        last_human = next((m for m in reversed(state["messages"])
                           if getattr(m, "type", "") == "human"), None)
        if last_human is None:
            return ""
        return await recall_prompt(str(last_human.content))
    except Exception:  # noqa: BLE001 — 记忆失败不阻断主流程
        return ""


class DevFakeChatModel(BaseChatModel):
    """开发回退假模型（DEV_FAKE_MODEL=true）：无需 API Key 即可验证
    SSE 流式、工具调用与持久化全链路。行为：
    - 消息含"写入/保存/修改" -> 调用 write_file（高风险，验证 Harness DENY/HITL 链路）
    - 消息含"通知/发送/notify" -> 调用 demo-notify__send_message（高风险，HITL 验证）
    - 消息含"读取/readme/文件/总结" -> 调用 read_text_file 读 workspace/readme.txt
    - 消息含"静默/空白" -> 空输出结束（验证完成门控 gate_blocked）
    - 消息含"降级/故障" -> 抛非瞬态异常（验证 fallback chain）
    - 消息含"重试/限流 <nonce>" -> 首次抛瞬态异常、重试成功（验证 tenacity 重试）
    - 已有工具结果 -> 输出总结文本
    - 其它 -> 输出问候文本
    chunk 附带 usage_metadata，使 token 预算强制可确定性验证。
    """

    tools: list[Any] = Field(default_factory=list)
    is_fallback: bool = False  # 降级链中的备用模型：始终成功（用于 fallback 验收）

    def bind_tools(self, tools, **kwargs):
        return DevFakeChatModel(tools=tools, is_fallback=self.is_fallback)

    def _has_tool(self, suffix: str) -> bool:
        return any(getattr(t, "name", "").endswith(suffix) for t in (self.tools or []))

    @staticmethod
    def _last_text(messages: list) -> str:
        last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        return last_human.content if last_human else ""

    def _reply(self, messages: list) -> AIMessage:
        from pathlib import Path

        from app.config import MCP_FILESYSTEM_ABS_DIR

        last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        text = (last_human.content if last_human else "")
        # 通道路由：router 节点用固定标记提示假模型输出结构化判定
        if any("任务路由器" in str(getattr(m, "content", "")) for m in messages):
            channel, reason, clarify = heuristic_route(text)
            payload: dict = {"channel": channel, "reason": reason, "clarify": clarify}
            # fast 且确需一次取数：给出确定性动作（模拟结构化输出「要执行什么」）
            if channel == "fast" and not clarify and any(k in text for k in _FAKE_METRIC_KW):
                payload["action"] = {"tool": "nl2sql__run_sql", "args": {"sql": _FAKE_SALES_SQL}}
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))
        # 通道路由：clarify 直接反问；wrap_up 做一次纯文字收尾（均不调工具）
        if any("【澄清】" in str(getattr(m, "content", "")) for m in messages):
            return AIMessage(content="请补充关键参数（例如时间范围、地区或指标），我再为你取数。")
        if any("【收尾】" in str(getattr(m, "content", "")) for m in messages):
            return AIMessage(content="预算已用尽，基于已获取的信息给出结论（DEV_FAKE_MODEL 收尾答复）。")
        has_tool_result = any(getattr(m, "type", "") == "tool" for m in messages)
        # 通道路由验收：模型显式申请升级（request_deep 只在 calc 通道绑定）
        if "申请升级" in text and not has_tool_result and self._has_tool("request_deep"):
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "request_deep", "args": {},
            }])
        # 通道路由验收：单次工具结果过大（>8000 字符）触发 result_too_large
        if "大结果" in text and not has_tool_result and self._has_tool("sqlite-query__query"):
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "sqlite-query__query",
                "args": {"sql": "select hex(zeroblob(5000)) as big"},
            }])
        # Harness Phase 3.2：循环触发词——无视历史工具结果，持续产出同一调用
        # （必须在 NL2SQL 取数分支之前：否则一次查询就收敛，无法验证循环检测）
        if "循环" in text:
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "sqlite-query__query",
                "args": {"sql": "select 'loop' as x"},
            }])
        # 通道路由验收：持续发起唯一参数的调用（覆盖 deep 步数耗尽 -> wrap_up）。
        # 扫全部消息：长任务会被上下文治理压缩，原始 Human 可能只留在摘要里。
        if any("长任务" in str(getattr(m, "content", "")) for m in messages):
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "sqlite-query__query",
                "args": {"sql": f"select '{uuid4hex()}' as x"},
            }])
        # 通道升级验收：稳定失败的 SQL（连续 2 次失败触发 tool_failed_twice）
        if "SQL错误" in text:
            errs = sum(1 for m in messages
                       if getattr(m, "type", "") == "tool" and "ERROR" in str(m.content))
            if errs < 2:
                return AIMessage(content="", tool_calls=[{
                    "id": f"call_dev_{uuid4hex()}", "name": "nl2sql__run_sql",
                    "args": {"sql": "SELECT * FROM not_a_table"},
                }])
            return AIMessage(content="SQL 连续两次执行失败，已升级通道仍不可用，请检查表名后重试。")
        # 通道升级验收：连续被 AuthorityController 拒绝 2 次触发 guard_denied_twice
        if "重试拒绝" in text:
            denials = sum(1 for m in messages
                          if getattr(m, "type", "") == "tool"
                          and str(m.content).startswith(DENIED_PREFIX))
            if denials < 2:
                return AIMessage(content="", tool_calls=[{
                    "id": f"call_dev_{uuid4hex()}", "name": "filesystem__read_text_file",
                    "args": {"path": str(Path(MCP_FILESYSTEM_ABS_DIR) / "readme.txt")},
                }])
            return AIMessage(content="连续两次被拒，已停止重试并切换策略。")
        # NL2SQL 场景：报告类任务引用真实 query_id（完成门控断言出处）
        if "报告" in text and self._has_tool("__write_report"):
            last_tool = next((m for m in reversed(messages)
                              if getattr(m, "type", "") == "tool"), None)
            body = str(last_tool.content) if last_tool else ""
            # 从全部工具输出里找已执行查询的 query_id（最后一条可能是写报告的回执）
            known = [q for m in messages if getattr(m, "type", "") == "tool"
                     for q in re.findall(r"q_[0-9a-f]{8}", str(m.content))]
            cite = known[-1] if known else ""
            if "报告已写入" in body:
                return AIMessage(content=(f"报告已生成，数据来源 {cite or '（未引用查询）'}。"
                                          "（DEV_FAKE_MODEL 演示答复）"))
            if not cite:
                # 还没有可取数的结果：先跑查询，再写报告
                if self._has_tool("__run_sql"):
                    return AIMessage(content="", tool_calls=[{
                        "id": f"call_dev_{uuid4hex()}", "name": "nl2sql__run_sql",
                        "args": {"sql": ("SELECT strftime('%Y-%m', o.created_at) AS month, "
                                         "SUM(oi.quantity * oi.unit_price) AS sales "
                                         "FROM orders o JOIN order_items oi ON oi.order_id = o.id "
                                         "GROUP BY month ORDER BY month")},
                    }])
                return AIMessage(content="")
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "nl2sql__write_report",
                "args": {"title": "经营分析报告",
                         "markdown": f"# 经营分析报告\n\n数据来源：{cite}\n\n"
                                     f"```\n{body[:400]}\n```"},
            }])
        # NL2SQL 场景：单指标/多步取数
        if any(k in text for k in ("销售额", "订单", "统计", "查询", "品类", "排名", "趋势")) \
                and self._has_tool("__run_sql") and not has_tool_result:
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "nl2sql__run_sql",
                "args": {"sql": _FAKE_SALES_SQL},
            }])
        if has_tool_result:
            tool_msg = next(m for m in reversed(messages) if getattr(m, "type", "") == "tool")
            return AIMessage(content=f"根据文件内容：{str(tool_msg.content)[:200]}\n\n这是一个演示答复（DEV_FAKE_MODEL 模式，未接真实 LLM）。")
        # Harness Phase 3.1：写对账触发词——写固定文件，args 带 flaky nonce，
        # 首次超时后由对账判定副作用（文件不存在->安全重试；已存在->视为成功）
        if "对账" in text:
            m = re.search(r"flaky\s*([0-9a-zA-Z]{4,32})", text)
            nonce = m.group(1) if m else uuid4hex()
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "filesystem__write_file",
                "args": {"path": str(Path(MCP_FILESYSTEM_ABS_DIR) / "reconcile_demo.txt"),
                         "content": f"对账写入 flaky {nonce}：{text[:60]}"},
            }])
        # Harness Phase 3.1：工具瞬态重试触发词——args 带 flaky nonce，
        # 由 recovery._inject_flaky 首次抛 TimeoutError、退避重试后真实执行
        if "工具重试" in text or "flaky" in text:
            m = re.search(r"flaky\s*([0-9a-zA-Z]{4,32})", text)
            nonce = m.group(1) if m else uuid4hex()
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "sqlite-query__query",
                "args": {"sql": f"select 'flaky {nonce}' as x"},
            }])
        # M2 热插拔验收：调用动态注册的 echo-test Server 工具
        if "echo" in text.lower():
            m = re.search(r"text\s*(?:为|=|:)\s*(\S+)", text)
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "echo-test__echo",
                "args": {"text": m.group(1) if m else "hello"},
            }])
        if any(k in text for k in ("静默", "空白")):
            return AIMessage(content="")  # 完成门控测试：空输出结束
        if any(k in text for k in ("通知", "发送", "notify")):
            # 触发高风险工具，用于 HITL 链路验证
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "demo-notify__send_message",
                "args": {"recipient": "ops", "message": f"来自演示模型的通知：{text[:50]}"},
            }])
        if any(k in text for k in ("写入", "保存", "修改")):
            # 触发写文件工具，用于 Harness DENY（契约限权）与契约内 HITL 验证
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "filesystem__write_file",
                "args": {"path": str(Path(MCP_FILESYSTEM_ABS_DIR) / "harness_demo.txt"),
                         "content": f"来自演示模型：{text[:50]}"},
            }])
        if any(k in text for k in ("读取", "readme", "文件", "总结")):
            return AIMessage(content="", tool_calls=[{
                "id": f"call_dev_{uuid4hex()}", "name": "filesystem__read_text_file",
                "args": {"path": str(Path(MCP_FILESYSTEM_ABS_DIR) / "readme.txt")},
            }])
        return AIMessage(content=f"你好！我是 AgentFlow 演示智能体（DEV_FAKE_MODEL 模式）。收到：「{text}」。配置 OPENAI_API_KEY 后即可接入真实模型。")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        msg = self._reply(messages)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    @staticmethod
    def _usage(msg: AIMessage) -> dict:
        out = max(1, len(msg.content) // 4)
        return {"input_tokens": 40, "output_tokens": out, "total_tokens": 40 + out}

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        msg = self._reply(messages)
        if not msg.content:  # 工具调用轮：无文本，直接产出完整 chunk（含 tool_calls）
            yield ChatGenerationChunk(message=AIMessageChunk(
                content="", tool_calls=msg.tool_calls,
                usage_metadata=self._usage(msg)))
            return
        pieces = [msg.content[i:i + 6] for i in range(0, len(msg.content), 6)]
        for i, piece in enumerate(pieces):
            kwargs = {"usage_metadata": self._usage(msg)} if i == len(pieces) - 1 else {}
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece, **kwargs))

    @property
    def _llm_type(self) -> str:
        return "dev-fake"

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        """异步流式：叠加 Harness Phase 2 的故障注入（tenacity 重试 / fallback 触发）。"""
        text = self._last_text(messages)
        if not self.is_fallback:
            if any(k in text for k in ("降级", "故障")):
                raise ModelUnavailableError("模拟主模型不可用（DEV_FAKE_MODEL）：触发 fallback chain")
            if any(k in text for k in ("重试", "限流")):
                m = _RETRY_NONCE_RE.search(text)
                nonce = m.group(1) if m else text
                if nonce not in _seen_nonces:
                    _seen_nonces.add(nonce)
                    raise TimeoutError("模拟瞬时限流（DEV_FAKE_MODEL）：触发 tenacity 重试")
        for chunk in self._stream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield chunk


def resolve_provider(model_id: str | None) -> str:
    """按名前缀解析 provider（与 build_model 同源，供 /models 注册表复用）。"""
    mid = model_id or ""
    return "anthropic" if mid.startswith("claude") else "openai"


def build_model(model_id: str | None = None, *, is_fallback: bool = False) -> BaseChatModel:
    from app.config import settings

    if settings.dev_fake_model:
        return DevFakeChatModel(is_fallback=is_fallback)

    mid = model_id or settings.default_model
    kwargs: dict[str, Any] = {}
    provider = resolve_provider(mid)
    if provider == "anthropic":
        kwargs["api_key"] = settings.anthropic_api_key
    elif mid.startswith("deepseek"):
        # DeepSeek 走 OpenAI 兼容协议，使用独立 Key 与 Base URL
        kwargs["api_key"] = settings.deepseek_api_key
        kwargs["openai_api_base"] = "https://api.deepseek.com/v1"
        kwargs["stream_usage"] = True
    else:
        if settings.openai_base_url:
            kwargs["openai_api_base"] = settings.openai_base_url
        kwargs["api_key"] = settings.openai_api_key
        kwargs["stream_usage"] = True  # 流式响应附带 usage（DashScope 兼容模式需要）
    return init_chat_model(mid, model_provider=provider, temperature=0.3, **kwargs)


def _budget_hint(state: AgentState) -> str | None:
    """Harness Phase 2：把剩余预算注入推理上下文，让模型主动收敛。

    瞬态提示（节点内构造，不写入 state，故每轮可刷新且不膨胀消息）。
    无预算配置且无 tracker 时返回 None（回归兼容）。
    """
    from app.harness.budget import get_current_budget

    parts: list[str] = []
    tracker = get_current_budget()
    if tracker is not None:
        rem = tracker.tokens_remaining()
        if rem is not None:
            parts.append(f"token 剩余约 {rem}")
    contract = _contract_of(state)
    used = state.get("tool_calls_count", 0)
    if contract.budgets.max_tool_calls is not None:
        parts.append(f"工具调用剩余 {max(0, contract.budgets.max_tool_calls - used)} 次")
    if contract.budgets.max_iterations is not None:
        parts.append(f"迭代剩余 {max(0, contract.budgets.max_iterations - used)} 轮")
    if not parts:
        return None
    return ("【预算提示】" + "；".join(parts) + "。请在预算内优先收敛，必要时直接给出结论。")


def build_graph(tools: list[BaseTool], checkpointer: Any, model_id: str | None = None,
                tool_risks: dict[str, str] | None = None) -> Any:
    """构建并编译带 HITL 门控的 ReAct 图。tools/checkpointer 由调用方注入。

    tool_risks: 限定名（{server}__{tool}）-> low|medium|high，来自注册表缓存。
    """
    from app.config import fallback_ids
    from app.config import settings as _settings

    primary_id = model_id or _settings.default_model
    # 模型链：primary + 注册表配置的降级备用（Harness Phase 2 Policy）。
    # 不在此绑定工具——通道不同工具集不同，绑定放到 agent 节点内按 run 决定。
    raw_chain: list[tuple[str, Any]] = [(primary_id, build_model(model_id))]
    for fid in fallback_ids(primary_id):
        raw_chain.append((fid, build_model(fid, is_fallback=True)))
    tool_map = {t.name: t for t in tools}
    risks = tool_risks or {}

    async def agent_node(state: AgentState) -> dict:
        from langchain_core.messages import RemoveMessage

        from app.config import registry_entry

        msgs = state["messages"]
        if not any(isinstance(m, SystemMessage) for m in msgs):
            from app.config import MCP_FILESYSTEM_ABS_DIR

            sys_content = (
                DEFAULT_SYSTEM
                + f"\n当前 filesystem 工具可访问的工作区绝对路径为：{MCP_FILESYSTEM_ABS_DIR}"
                + "（例如读取工作区内文件时，path 参数应写成该路径下的绝对路径）。"
            )
            msgs = [SystemMessage(content=sys_content), *msgs]
        # Harness Phase 3.3：上下文治理（熵管理）。触发时把 RemoveMessage+摘要写入 state
        # （随 checkpoint 持久化），并把事件累积到 context_events 供 runtime 转 SSE。
        gov_updates: list = []
        ctx_events = list(state.get("context_events") or [])
        entry = registry_entry(model_id or _settings.default_model)
        cw = int((entry or {}).get("context_window") or 0)
        governed = maybe_govern(msgs, context_window=cw)
        if governed:
            gov_updates, strategy = governed
            removed = {u.id for u in gov_updates if isinstance(u, RemoveMessage)}
            summaries = [u for u in gov_updates if not isinstance(u, RemoveMessage)]
            head = msgs[:1] if getattr(msgs[0], "type", "") == "system" else []
            rest = [m for m in msgs[len(head):] if getattr(m, "id", None) not in removed]
            msgs = [*head, *summaries, *rest]
            ctx_events.append({"strategy": strategy,
                               "before_msgs": len(state["messages"]),
                               "after_msgs": len(msgs)})
        # Harness Phase 2：注入剩余预算（瞬态，不落 state）
        hint = _budget_hint(state)
        if hint:
            msgs = [SystemMessage(content=hint), *msgs]
        # 通道路由：注入通道预算提示；clarify 时要求直接反问、不调工具
        channel = state.get("channel") or "deep"
        c_hint = _channel_hint(state)
        if state.get("clarify"):
            c_hint = (c_hint or "") + "\n【澄清】用户缺少关键参数，请直接提出一个澄清问题，不要调用工具。"
        if state.get("wrap_up"):
            c_hint = (c_hint or "") + "\n【收尾】通道预算已用尽，请基于已有信息直接给出最终答复，不要再调用工具。"
        if c_hint:
            msgs = [SystemMessage(content=c_hint), *msgs]
        # Harness Phase 4：长期记忆召回注入（瞬态，不落 state；开关关闭时为空）
        memory_text = await _recall_memory(state)
        if memory_text:
            msgs = [SystemMessage(content=memory_text), *msgs]
        # 通道决定本轮可绑定工具集（fast 恒为空；wrap_up 清空工具做纯文字收尾）
        bound_tools = tools_for_channel(tools, channel, risks,
                                        wrap_up=bool(state.get("wrap_up")),
                                        clarify=bool(state.get("clarify")))
        # calc 通道额外挂 request_deep：模型可显式申请升级（§6 model_requested）
        if (channel == "calc" and bound_tools
                and not state.get("channel_locked")):
            bound_tools = [*bound_tools, REQUEST_DEEP_TOOL]
        call_chain = [(mid, m.bind_tools(bound_tools)) for mid, m in raw_chain]
        # 用 astream 聚合，保证 on_chat_model_stream 事件可被 SSE 转译捕获；
        # 经 Policy 链（tenacity 重试 + fallback），结果写入 model_fallbacks
        ai: AIMessage | None = None
        fallbacks: list[dict] = []
        result: dict = {}
        async for chunk in astream_with_policy(call_chain, msgs, result=result, fallbacks=fallbacks):
            ai = chunk if ai is None else ai + chunk
        if ai is None:
            ai = AIMessage(content="")
        prev = state.get("model_fallbacks") or {}
        out: dict = {"messages": [*gov_updates, ai], "model_fallbacks": {
            "model_used": result.get("model_used") or prev.get("model_used"),
            "fallbacks": (prev.get("fallbacks") or []) + fallbacks,
        }, "step_count": state.get("step_count", 0) + 1}
        if ctx_events != (state.get("context_events") or []):
            out["context_events"] = ctx_events
        return out

    async def tools_node(state: AgentState) -> dict:
        from langchain_core.messages import ToolMessage

        last = state["messages"][-1]
        # 改参覆盖：wait_approval 批准时可能替换参数（按 call_id 匹配）
        edits: dict[str, dict] = {}
        pa = state.get("pending_approval")
        ar = state.get("approval_result")
        if pa and ar and ar.get("decision") == "edit" and ar.get("edited_args") is not None:
            edits[pa["call_id"]] = ar["edited_args"]
        # Harness 拒绝：authority_gate 裁决 DENY 的调用不执行，拒绝消息回喂模型
        denied_map = {d["call_id"]: d for d in (state.get("denied_calls") or [])}
        out = []
        # Harness Phase 3.2：本轮实际执行调用的 action+params 哈希（循环检测）
        new_hashes = list(state.get("recent_call_hashes") or [])
        n_denied = n_ok = n_err = 0
        requested_deep = False
        too_large = False
        for call in getattr(last, "tool_calls", []) or []:
            if call["id"] in denied_map:
                n_denied += 1
                out.append(ToolMessage(
                    content=(f"{DENIED_PREFIX}：{denied_map[call['id']]['reason']}。"
                             "请勿重试该工具，改用契约允许的方式或直接向用户说明。"),
                    tool_call_id=call["id"]))
                continue
            name = call["name"]
            # 模型显式申请升级：合成工具，不执行副作用，只置升级标记（§6 model_requested）
            if name == REQUEST_DEEP_NAME:
                requested_deep = True
                out.append(ToolMessage(content="已申请升级到 deep 通道。",
                                       tool_call_id=call["id"], name=name))
                continue
            tool = tool_map.get(name)
            args = edits.get(call["id"], call["args"])
            if tool is None:
                n_err += 1
                out.append(ToolMessage(content=f"未知工具: {name}", tool_call_id=call["id"]))
                continue
            new_hashes.append(call_hash(name, args))
            # Harness Phase 3.1：分级恢复（瞬态重试 / 写操作对账），失败统一回喂文本
            text, status = await execute_with_recovery(tool, args, tool_name=name)
            # 业务性失败（工具以 ERROR 文本返回）与 runtime 的 tool_end 判定保持一致：
            # 也计入连续失败，作为通道升级的可观测事实。
            if status != "error" and str(text).lstrip().startswith("ERROR"):
                status = "error"
            if status == "error":
                n_err += 1
            else:
                n_ok += 1
            if status != "error" and result_size_exceeded(text):
                too_large = True  # 结果过大：留在 messages 升一级消化（§6 result_too_large）
            out.append(ToolMessage(content=text, tool_call_id=call["id"], name=name,
                                   status="error" if status == "error" else "success"))
        threshold = max(2, _loop_threshold())
        loop_detected = (len(new_hashes) >= threshold
                         and len(set(new_hashes[-threshold:])) == 1)
        # 通道升级只看可观测事实：连续 DENY / 连续工具失败（单次失败只回灌让模型自纠）
        deny_streak = (state.get("deny_streak", 0) + 1) if n_denied else 0
        if n_ok:
            tool_fail_streak = 0
        elif n_err:
            tool_fail_streak = state.get("tool_fail_streak", 0) + n_err
        else:
            tool_fail_streak = state.get("tool_fail_streak", 0)
        channel = state.get("channel") or "deep"
        locked = bool(state.get("channel_locked"))
        escalate, reason = False, None
        # 升级优先级：模型显式申请 > 连续拒绝 > 连续工具失败 > 单次结果过大
        if requested_deep and can_escalate(channel, locked):
            escalate, reason = True, "model_requested"
        elif deny_streak >= 2 and can_escalate(channel, locked):
            escalate, reason = True, "guard_denied_twice"
        elif tool_fail_streak >= 2 and can_escalate(channel, locked):
            escalate, reason = True, "tool_failed_twice"
        elif too_large and can_escalate(channel, locked):
            escalate, reason = True, "result_too_large"
        return {"messages": out, "tool_calls_count": state.get("tool_calls_count", 0) + 1,
                "pending_approval": None, "approval_result": None, "denied_calls": [],
                "recent_call_hashes": new_hashes[-threshold:],
                "loop_detected": loop_detected,
                "deny_streak": deny_streak, "tool_fail_streak": tool_fail_streak,
                "escalate_pending": escalate, "escalate_reason": reason}

    def authority_gate_node(state: AgentState) -> dict:
        """Harness 确定性权限门控（原 hitl_gate 扩展）。

        对本轮每个 tool_call 裁决（docs/01 FR-26）：
        - 通道预算（步数/时间/token）耗尽 -> 升级或 wrap_up（不执行本轮工具）
        - 超出工具调用预算 -> DENY（记入 denied_calls，不执行）
        - 黑名单/白名单外/风险超契约 -> DENY
        - 工具风险超出当前通道上限 -> 升级（fast 不挂工具、calc 不挂高危写）
        - 契约内 high 风险：当前通道允许审批则 HITL，否则升级
        其余 ALLOW。模型可以决定"做什么"，契约和通道决定"能不能做"。
        """
        channel = state.get("channel") or "deep"
        limit = limits_of(channel)
        locked = bool(state.get("channel_locked"))

        # 通道预算守卫：步数 / 时间 / token 任一超限 -> 升级（有上级）或收尾
        from app.harness.budget import get_current_budget

        tracker = get_current_budget()
        reason = None
        if state.get("step_count", 0) >= limit.max_steps:
            reason = "steps_exhausted"
        elif time.monotonic() - float(state.get("started_at") or time.monotonic()) \
                >= limit.time_budget_s:
            reason = "time_exhausted"
        elif tracker is not None and tracker.tokens_used >= limit.token_budget:
            reason = "token_exhausted"
        if reason:
            if can_escalate(channel, locked):
                return {"escalate_pending": True, "escalate_reason": reason,
                        "denied_calls": [], "pending_approval": None}
            return {"wrap_up": True, "escalate_pending": False, "denied_calls": [],
                    "pending_approval": None,
                    "budget_events": [*state.get("budget_events", []), {"reason": reason}]}

        last = state["messages"][-1]
        contract = _contract_of(state)
        denied: list[dict] = []
        pending = None
        escalate, esc_reason = False, None
        max_calls = contract.budgets.max_tool_calls
        used = state.get("tool_calls_count", 0)
        for idx, call in enumerate(getattr(last, "tool_calls", []) or []):
            if max_calls is not None and used + idx + 1 > max_calls:
                denied.append({"call_id": call["id"], "tool": call["name"],
                               "reason": f"超出工具调用预算（max_tool_calls={max_calls}）"})
                continue
            risk = risks.get(call["name"], "low")
            # 通道工具上限：fast 无工具、calc 不挂高危写 -> 升级而不是拒绝
            if not risk_allowed(channel, risk):
                if can_escalate(channel, locked):
                    escalate, esc_reason = True, ("approval_required" if risk == "high"
                                                  else "tool_risk_exceeded")
                else:
                    denied.append({"call_id": call["id"], "tool": call["name"],
                                   "reason": f"工具风险 {risk} 超出通道 {channel} 上限"})
                continue
            decision = authority.check(call["name"], risk, contract)
            if decision is authority.Decision.DENY:
                denied.append({"call_id": call["id"], "tool": call["name"],
                               "reason": authority.deny_reason(call["name"], risk, contract)
                                         or "超出任务契约范围"})
            elif decision is authority.Decision.REQUIRE_HITL and pending is None:
                if limit.allow_human_approval:
                    pending = {"call_id": call["id"], "tool": call["name"],
                               "args": call["args"], "risk": risk}
                elif can_escalate(channel, locked):
                    escalate, esc_reason = True, "approval_required"
        if escalate:
            return {"escalate_pending": True, "escalate_reason": esc_reason,
                    "denied_calls": denied, "pending_approval": None}
        return {"pending_approval": pending, "denied_calls": denied,
                "escalate_pending": False}

    def wait_approval_node(state: AgentState) -> dict:
        """HITL 恢复点：interrupt 暂停（状态由 Checkpointer 持久化，可跨进程重启）。

        resume 值：{decision: approve|edit|reject, edited_args, reason}。
        拒绝时生成拒绝 ToolMessage 直接回喂 agent（不执行工具）。
        """
        pa = state["pending_approval"]
        payload = {
            "interrupt_id": f"int_{uuid4hex()}", "tool": pa["tool"], "args": pa["args"],
            "risk": pa["risk"],
            "message": f"智能体请求执行高风险操作：{pa['tool']}",
            "expires_in": 600,  # 10min 超时自动拒绝（docs/04 §2）
        }
        decision = interrupt(payload)
        if not isinstance(decision, dict):
            decision = {"decision": "reject", "reason": "无效的确认响应"}
        if decision.get("decision") in ("approve", "edit"):
            return {"approval_result": decision}
        from langchain_core.messages import ToolMessage

        reason = decision.get("reason") or "用户拒绝"
        # 拒绝路径：为待确认调用与本轮被 Harness 拒绝的调用一并生成回喂消息，
        # 避免同轮多个 tool_call 出现悬挂（ unanswered tool_calls 会导致下轮模型调用报协议错误）
        out = [ToolMessage(
            content=f"用户拒绝了该操作（原因：{reason}）。请不要重试，改用其它方式或直接向用户说明。",
            tool_call_id=pa["call_id"])]
        for d in (state.get("denied_calls") or []):
            out.append(ToolMessage(
                content=f"{DENIED_PREFIX}：{d['reason']}。请勿重试该工具，改用契约允许的方式或直接向用户说明。",
                tool_call_id=d["call_id"]))
        return {
            "messages": out,
            "pending_approval": None, "approval_result": None, "denied_calls": [],
            "tool_calls_count": state.get("tool_calls_count", 0) + 1,
        }

    async def router_node(state: AgentState) -> dict:
        """入口判定：显式信号优先，否则一次结构化输出（不挂工具）。

        五种去向里 reuse/plan 依赖 checkpoint/HITL 语义，当前以 direct/escalate/clarify
        为主：clarify 直接反问不烧预算，其余按通道进入执行循环。

        direct（fast）且判定给出 action 时，由**程序**执行这一次只读取数（不挂工具 schema、
        不进工具循环），结果以 ToolMessage 回喂，agent 随后据结果作答（§5.4 direct）。
        """
        from langchain_core.messages import ToolMessage

        requested = state.get("requested_channel")
        text = next((str(m.content) for m in reversed(state["messages"])
                     if getattr(m, "type", "") == "human"), "")
        if requested in ("fast", "calc", "deep"):
            decision = {"decision": "escalate" if requested != "fast" else "direct",
                        "channel": requested, "reason": "explicit", "clarify": False,
                        "action": None}
            locked = True
        else:
            decision = await route_decision(raw_chain[0][1], text,
                                            _tools_hint(tools, risks))
            locked = False
        out: dict = {"channel": decision["channel"], "channel_locked": locked,
                     "clarify": decision["clarify"], "route_decision": decision,
                     "step_count": 0, "started_at": time.monotonic(),
                     "escalations": [], "escalate_pending": False, "escalate_reason": None,
                     "deny_streak": 0, "tool_fail_streak": 0}
        action = decision.get("action")
        if not action:
            return out
        name = str(action.get("tool") or "")
        args = action.get("args") or {}
        tool = tool_map.get(name)
        risk = risks.get(name, "low")
        contract = _contract_of(state)
        # fast 只放行低风险只读取数；其余一律升档，交给 calc 走正常工具循环
        if (tool is not None and risk == "low"
                and authority.check(name, risk, contract) is authority.Decision.ALLOW):
            result_text, status = await execute_with_recovery(tool, args, tool_name=name)
            ok = status != "error" and not str(result_text).lstrip().startswith("ERROR")
            if not ok and can_escalate(decision["channel"], locked):
                # fast 只有一次取数机会，失败即失去试错能力（§2）：升档让模型带工具重试
                out["escalate_pending"] = True
                out["escalate_reason"] = "fast_action_failed"
                return out
            call_id = f"call_route_{uuid4hex()}"
            # 合成一轮工具调用：协议要求 ToolMessage 前有带 tool_calls 的 AIMessage
            out["messages"] = [
                AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": args}]),
                ToolMessage(content=result_text, tool_call_id=call_id, name=name,
                            status="error" if not ok else "success")]
            out["tool_calls_count"] = state.get("tool_calls_count", 0) + 1
            out["route_decision"] = {**decision, "action_ran": True}
            return out
        if can_escalate(decision["channel"], locked):
            out["escalate_pending"] = True
            out["escalate_reason"] = "tool_risk_exceeded"
        return out

    def route_after_router(state: AgentState) -> str:
        # fast 的确定性取数无法在本通道执行（非低风险/未授权）-> 升档走正常循环
        return "escalate" if state.get("escalate_pending") else "guard"

    def guard_node(state: AgentState) -> dict:
        """每轮模型调用前的通道预算守卫：步数 / 时间 / token 任一超限即升级或收尾。"""
        if state.get("wrap_up"):
            return {"escalate_pending": False}
        channel = state.get("channel") or "deep"
        limit = limits_of(channel)
        from app.harness.budget import get_current_budget

        tracker = get_current_budget()
        reason = None
        if state.get("step_count", 0) >= limit.max_steps:
            reason = "steps_exhausted"
        elif time.monotonic() - float(state.get("started_at") or time.monotonic()) \
                >= limit.time_budget_s:
            reason = "time_exhausted"
        elif tracker is not None and tracker.tokens_used >= limit.token_budget:
            reason = "token_exhausted"
        if not reason:
            return {"escalate_pending": False}
        if can_escalate(channel, bool(state.get("channel_locked"))):
            return {"escalate_pending": True, "escalate_reason": reason}
        return {"wrap_up": True, "escalate_pending": False,
                "budget_events": [*state.get("budget_events", []), {"reason": reason}]}

    def escalate_node(state: AgentState) -> dict:
        """单向升级：只换通道参数，不改上下文、不回滚已执行结果。

        路由回到 authority_gate：同一批 tool_calls 在新通道下重新裁决（不重跑模型）。
        """
        frm = state.get("channel") or "deep"
        to = next_channel(frm)
        if to is None:
            return {"wrap_up": True, "escalate_pending": False}
        record = {
            "from": frm, "to": to,
            "reason": state.get("escalate_reason") or "escalated",
            "at_step": state.get("step_count", 0),
            "elapsed_s": round(time.monotonic()
                               - float(state.get("started_at") or time.monotonic()), 1),
        }
        channel = to
        # fast -> calc 时若契约未设 token 上限，预算记账随通道放宽
        tracker = None
        from app.harness.budget import get_current_budget

        tracker = get_current_budget()
        contract = _contract_of(state)
        if tracker is not None and contract.budgets.max_tokens is None:
            with tracker._lock:
                tracker.budgets.max_tokens = limits_of(channel).token_budget
        return {"channel": channel, "escalations": [*(state.get("escalations") or []), record],
                "escalate_pending": False, "escalate_reason": None,
                "deny_streak": 0, "tool_fail_streak": 0}

    def wrap_up_node(state: AgentState) -> dict:
        """到顶收尾：为悬挂 tool_calls 补回喂消息，清空工具做一次纯文字作答。"""
        from langchain_core.messages import ToolMessage

        last = state["messages"][-1]
        out = []
        denied_map = {d["call_id"]: d for d in (state.get("denied_calls") or [])}
        for call in (getattr(last, "tool_calls", None) or []):
            reason = (denied_map.get(call["id"]) or {}).get("reason")
            text = (f"通道预算已用尽（{reason}），本次调用未执行。"
                    "请基于已有信息直接给出最终答复，不要再调用工具。")
            out.append(ToolMessage(content=text, tool_call_id=call["id"]))
        return {"messages": out, "wrap_up": True, "escalate_pending": False,
                "escalate_reason": None, "denied_calls": [], "pending_approval": None}

    def gate_node(state: AgentState) -> dict:
        """完成门控前置判定：证据不足且还有上级通道时先升级再判（§7）。

        - 仅在自然结束（无 tool_calls）时进入；wrap_up / clarify 由路由直接结束
        - 只对"证据类"未通过项升级（invariant:*）：空输出 / 预算超限属于任务失败，
          仍交由 runtime 判 gate_blocked，避免把"做不出来"误当成"证据不够"
        - 升级不重跑模型：messages 原样带到新通道继续取证据
        """
        if state.get("wrap_up") or state.get("clarify"):
            return {}
        contract = _contract_of(state)
        if not contract.gates_enabled:
            return {}
        from langchain_core.messages import ToolMessage

        msgs = state.get("messages") or []
        output = next((str(m.content) for m in reversed(msgs)
                       if isinstance(m, AIMessage)), "")
        # 交付物正文（write_report 的 markdown）——报告出处写在文件里而非对话答复里
        deliverable = "\n".join(
            str((c.get("args") or {}).get("markdown", ""))
            for m in msgs if isinstance(m, AIMessage)
            for c in (getattr(m, "tool_calls", None) or [])
            if str(c.get("name", "")).endswith("write_report")
            and isinstance(c.get("args"), dict))
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        executed = [m for m in tool_msgs
                    if getattr(m, "status", None) in ("success", "error")]
        from app.harness.budget import get_current_budget

        tracker = get_current_budget()
        result = evaluate_gates(
            output=output, tool_total=len(executed),
            tool_failed=sum(1 for m in executed if getattr(m, "status", None) == "error"),
            tokens_used=tracker.tokens_used if tracker is not None else 0,
            contract=contract,
            denied_total=sum(1 for m in tool_msgs
                             if str(m.content).startswith(DENIED_PREFIX)),
            succeeded_tools=[getattr(m, "name", "") or "" for m in executed
                             if getattr(m, "status", None) == "success"],
            tool_outputs=[str(m.content) for m in tool_msgs],
            deliverable=deliverable)
        evidence_failed = any(c["name"].startswith("invariant:") and not c["passed"]
                              for c in result.checked)
        if evidence_failed and can_escalate(state.get("channel") or "deep",
                                            bool(state.get("channel_locked"))):
            return {"escalate_pending": True,
                    "escalate_reason": "gate_insufficient_evidence"}
        return {}

    def route_after_guard(state: AgentState) -> str:
        return "escalate" if state.get("escalate_pending") else "agent"

    def route_after_agent(state: AgentState) -> str:
        from app.config import settings

        last = state["messages"][-1]
        if not getattr(last, "tool_calls", None):
            if state.get("wrap_up") or state.get("clarify"):
                return END
            return "gate"
        if state.get("wrap_up") or state.get("clarify"):
            return END
        # 迭代上限：契约预算优先，未配置回退全局 settings.max_iterations
        limit = settings.max_iterations
        contract = _contract_of(state)
        if contract.budgets.max_iterations is not None:
            limit = contract.budgets.max_iterations
        if state.get("tool_calls_count", 0) < limit:
            return "authority_gate"
        return END

    def route_after_completion_gate(state: AgentState) -> str:
        return "escalate" if state.get("escalate_pending") else END

    def route_after_gate(state: AgentState) -> str:
        if state.get("escalate_pending"):
            return "escalate"
        if state.get("wrap_up"):
            return "wrap_up"
        return "wait_approval" if state.get("pending_approval") else "tools"

    def route_after_approval(state: AgentState) -> str:
        ar = state.get("approval_result")
        return "tools" if ar and ar.get("decision") in ("approve", "edit") else "agent"

    def route_after_tools(state: AgentState) -> str:
        # Harness Phase 3.2：连续重复调用（action+params 哈希）-> 强制终止，防失控
        if state.get("loop_detected"):
            return END
        if state.get("escalate_pending"):
            return "escalate"
        return "guard"

    def route_after_escalate(state: AgentState) -> str:
        # 同一批 tool_calls 在新通道下重新裁决；否则回到预算守卫
        last = state["messages"][-1]
        return "authority_gate" if getattr(last, "tool_calls", None) else "guard"

    g = StateGraph(AgentState)
    g.add_node("router", router_node)
    g.add_node("guard", guard_node)
    g.add_node("agent", agent_node)
    g.add_node("gate", gate_node)
    g.add_node("authority_gate", authority_gate_node)
    g.add_node("wait_approval", wait_approval_node)
    g.add_node("escalate", escalate_node)
    g.add_node("wrap_up", wrap_up_node)
    g.add_node("tools", tools_node)
    g.add_edge(START, "router")
    g.add_conditional_edges("router", route_after_router,
                            {"escalate": "escalate", "guard": "guard"})
    g.add_conditional_edges("guard", route_after_guard,
                            {"escalate": "escalate", "agent": "agent"})
    g.add_conditional_edges("agent", route_after_agent,
                            {"authority_gate": "authority_gate", "gate": "gate", END: END})
    g.add_conditional_edges("gate", route_after_completion_gate,
                            {"escalate": "escalate", END: END})
    g.add_conditional_edges("authority_gate", route_after_gate,
                            {"escalate": "escalate", "wrap_up": "wrap_up",
                             "wait_approval": "wait_approval", "tools": "tools"})
    g.add_conditional_edges("wait_approval", route_after_approval,
                            {"tools": "tools", "agent": "agent"})
    g.add_conditional_edges("tools", route_after_tools,
                            {"escalate": "escalate", "guard": "guard", END: END})
    g.add_conditional_edges("escalate", route_after_escalate,
                            {"authority_gate": "authority_gate", "guard": "guard"})
    g.add_edge("wrap_up", "agent")
    return g.compile(checkpointer=checkpointer)
