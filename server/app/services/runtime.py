"""Agent 运行时服务：管理 MCP 连接、Checkpointer 与图的单例生命周期，
并把 LangGraph 流式事件转译为前端 SSE 事件（契约见 docs/05-API设计.md）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from app.agent.graph import build_graph
from app.agent.mcp_bridge import MCPServerConnection, to_langchain_tools
from app.agent.risk import classify
from app.config import MCP_FILESYSTEM_ABS_DIR, ROOT, settings
from app.db.base import SessionLocal
from app.harness import (
    DEFAULT_CHANNEL,
    DENIED_PREFIX,
    Budgets,
    BudgetTracker,
    TaskContract,
    graph_recursion_limit,
)
from app.harness.budget import current_budget
from app.harness.gates import evaluate as evaluate_gates
from app.models import MCPServer, Message, Run, ToolCall, User
from app.models import Session as ChatSession
from app.observability import (
    FALLBACK_USED,
    GEN_AI_AGENT_NAME,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_TOOL_NAME,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    NODE_NAME,
    RUN_ID,
    SESSION_ID,
    end_span,
    start_span,
)


class _AsyncSaverAdapter(BaseCheckpointSaver):
    """把同步 Checkpointer 适配为异步：用线程池执行同步方法。

    Windows 下 psycopg 异步与 Proactor 事件循环冲突，而 MCP stdio 子进程又需要
    Proactor，因此这里用同步 PostgresSaver + to_thread 提供 async 接口。
    继承 BaseCheckpointSaver 以通过 LangGraph 的类型校验；其余属性透传给底层 saver。
    """

    def __init__(self, sync_saver: Any) -> None:
        super().__init__(serde=sync_saver.serde)
        object.__setattr__(self, "_s", sync_saver)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_s"], name)

    async def aget_tuple(self, config: dict) -> Any:
        return await asyncio.to_thread(self._s.get_tuple, config)

    async def alist(self, config: dict, *, filter: Any = None,
                    before: Any = None, limit: int | None = None) -> Any:
        items = await asyncio.to_thread(
            lambda: list(self._s.list(config, filter=filter, before=before, limit=limit)))
        for it in items:
            yield it

    async def aput(self, config: dict, checkpoint: dict, metadata: dict,
                   new_versions: dict) -> dict:
        return await asyncio.to_thread(self._s.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config: dict, writes: list, task_id: str,
                          task_path: str = "") -> None:
        await asyncio.to_thread(self._s.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        if hasattr(self._s, "delete_thread"):
            await asyncio.to_thread(self._s.delete_thread, thread_id)


class Runtime:
    """进程级单例：FastAPI lifespan 中 setup()/teardown()。

    M2：MCP 连接池按 mcp_servers 表动态建连/断连（热插拔），
    stdio 崩溃自动重启（退避 3 次），故障 Server 仅剔除其工具不阻塞对话。
    """

    def __init__(self) -> None:
        self._stack: contextlib.AsyncExitStack | None = None
        self._conns: dict[str, MCPServerConnection] = {}      # name -> 连接
        self._expected: set[str] = set()                       # 预期存活的 server 名
        self._restarts: dict[str, deque[float]] = {}           # name -> 重启时间戳（退避窗口）
        self._pool_lock = asyncio.Lock()
        self.tools: list[Any] = []
        self.tool_risks: dict[str, str] = {}
        self.saver: AsyncPostgresSaver | None = None
        self._graph_cache: dict[str | None, Any] = {}
        self._active_runs: dict[str, asyncio.Task] = {}
        self._interrupts: dict[str, dict] = {}  # run_id -> {session_id, interrupt_id, ts}
        self._sweep_task: asyncio.Task | None = None

    # ---------------- MCP 连接池 ----------------

    async def setup(self) -> None:
        # 1) Checkpointer：
        #    - dev_sqlite: 内存（重启丢断点，仅开发）
        #    - Windows: 同步 PostgresSaver（psycopg 异步与 Proactor 事件循环冲突，
        #      而 MCP stdio 子进程又要求 Proactor；LangGraph 会自动在线程池调 sync saver）
        #    - 其他平台(Linux/mac): AsyncPostgresSaver
        import sys

        self._stack = contextlib.AsyncExitStack()
        if settings.dev_sqlite:
            from langgraph.checkpoint.memory import MemorySaver
            self.saver = MemorySaver()
            print("[runtime] 使用 MemorySaver 检查点（dev_sqlite 模式）")
        elif sys.platform == "win32":
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg_pool import ConnectionPool

            # autocommit：setup() 含 CREATE INDEX CONCURRENTLY，不能在事务块内
            pool = ConnectionPool(
                settings.checkpoint_db_url, max_size=10,
                kwargs={"autocommit": True, "prepare_threshold": 0},
                open=True,
            )
            self._cp_pool = pool
            saver = PostgresSaver(pool)
            saver.setup()
            self.saver = _AsyncSaverAdapter(saver)
            print("[runtime] 使用同步 PostgresSaver + 线程池异步适配（Windows 兼容模式）")
        else:
            self.saver = await self._stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(settings.checkpoint_db_url)
            )
            await self.saver.setup()
        # 2) MCP：确保内置 Server 注册表存在，然后按表建连
        await self._ensure_builtin_servers()
        await self.reload_from_db()
        # 3) HITL 超时巡检（docs/04 §2：超时注入"默认拒绝" resume）
        self._sweep_task = asyncio.create_task(self._sweep_expired_approvals(),
                                               name="hitl-sweeper")

    async def teardown(self) -> None:
        if self._sweep_task:
            self._sweep_task.cancel()
            self._sweep_task = None
        for task in self._active_runs.values():
            task.cancel()
        for name in list(self._conns):
            await self._disconnect(name)
        pool = getattr(self, "_cp_pool", None)
        if pool is not None:
            pool.close()
            self._cp_pool = None
        if self._stack:
            await self._stack.aclose()
            self._stack = None

    async def _ensure_builtin_servers(self) -> None:
        """首次启动写入内置 Server 注册行（filesystem / sqlite-query / demo-notify / nl2sql）。"""
        import sys
        from pathlib import Path

        ws = Path(MCP_FILESYSTEM_ABS_DIR)
        py = sys.executable
        builtins = [
            {"name": "filesystem", "command": "npx",
             "args": ["-y", "@modelcontextprotocol/server-filesystem", str(ws)]},
            {"name": "sqlite-query", "command": py,
             "args": [str(ROOT / "mcp-servers" / "sqlite-query" / "server.py")],
             "env": {"SQLITE_DB_PATH": str(ws / "demo.db")}},
            {"name": "demo-notify", "command": py,
             "args": [str(ROOT / "mcp-servers" / "demo-notify" / "server.py")],
             "env": {"NOTIFY_LOG_PATH": str(ws / "notifications.log")},
             "risk_overrides": {"send_message": "high"}},
            # NL2SQL 场景（通道路由验证负载）：合成电商数据集 + 5 个只读/交付工具。
            # write_report 为 medium：calc 及以上可写，fast 需升档（与通道风险上限一致）。
            {"name": "nl2sql", "command": py,
             "args": [str(ROOT / "mcp-servers" / "nl2sql" / "server.py")],
             "env": {"NL2SQL_DB_PATH": str(ws / "nl2sql.db"),
                     "NL2SQL_REPORT_DIR": str(ws / "reports")},
             "risk_overrides": {"write_report": "medium"}},
        ]
        async with SessionLocal() as db:
            rows = {r.name: r for r in
                    (await db.execute(select(MCPServer))).scalars().all()}
            for spec in builtins:
                row = rows.get(spec["name"])
                if row is None:
                    db.add(MCPServer(**spec))
                elif row.risk_overrides != spec.get("risk_overrides"):
                    # 内置 Server 的风险分级以代码为准：旧库残留的覆盖值需随版本同步，
                    # 否则同一工具在新旧环境中会得到不同的裁决路径（如误触发 HITL）。
                    row.risk_overrides = spec.get("risk_overrides")
            await db.commit()

    async def reload_from_db(self) -> None:
        """按 mcp_servers 表同步连接池：新增建连、禁用/删除断连、配置变更重连。"""
        async with SessionLocal() as db:
            rows = (await db.execute(select(MCPServer))).scalars().all()
        desired = {r.name: r for r in rows if r.enabled}
        async with self._pool_lock:
            # 断开：已连接但不再期望（禁用或删除）
            for name in list(self._conns):
                if name not in desired:
                    await self._disconnect(name)
            # 连接：期望但尚未连接
            for name, row in desired.items():
                if name not in self._conns:
                    await self._connect(row)
            # 配置变更（command/args/env 改变）：先断后建
            for name, row in desired.items():
                conn = self._conns.get(name)
                if conn is not None and conn._params != self._params_of(row):
                    await self._disconnect(name)
                    await self._connect(row)
        await self._rebuild_tools()

    @staticmethod
    def _params_of(row: MCPServer):
        from mcp import StdioServerParameters
        return StdioServerParameters(command=row.command or "",
                                     args=list(row.args or []), env=row.env or None)

    async def _connect(self, row: MCPServer) -> None:
        conn = MCPServerConnection(server_id=row.name, command=row.command or "",
                                   args=list(row.args or []), env=row.env or None)
        try:
            await conn.start()
        except Exception as e:
            await self._set_status(row.name, "disconnected", last_error=str(e)[:500])
            print(f"[mcp-pool] {row.name} 连接失败（降级剔除）: {e}")
            return
        self._conns[row.name] = conn
        self._expected.add(row.name)
        conn.add_exit_callback(lambda t: self._on_conn_exit(row.name, t))
        try:
            infos = await conn.list_tools()
            tools_meta = [{"name": i.name, "description": i.description,
                           "risk": classify(i.name, row.risk_overrides)} for i in infos]
            await self._set_status(row.name, "connected", tools_cache=tools_meta)
            print(f"[mcp-pool] {row.name} 已连接，工具: {[t['name'] for t in tools_meta]}")
        except Exception as e:
            await self._set_status(row.name, "degraded", last_error=str(e)[:500])

    async def _disconnect(self, name: str) -> None:
        conn = self._conns.pop(name, None)
        self._expected.discard(name)
        if conn:
            await conn.stop()
        await self._set_status(name, "disconnected")

    def _on_conn_exit(self, name: str, task: asyncio.Task) -> None:
        """后台 task 退出回调：预期内停止忽略，崩溃则退避重启。"""
        if name not in self._expected:
            return  # 主动断开（禁用/删除/teardown）
        if task.cancelled():
            err = "cancelled"
        else:
            exc = task.exception()
            err = repr(exc) if exc else "exited"
        print(f"[mcp-pool] {name} 连接退出（{err}），尝试重启")
        now = time.monotonic()
        hist = self._restarts.setdefault(name, deque())
        while hist and now - hist[0] > 60:
            hist.popleft()
        if len(hist) >= 3:
            asyncio.create_task(self._set_status(name, "degraded",
                                                 last_error=f"崩溃退避 3 次后放弃: {err}"))
            print(f"[mcp-pool] {name} 退避超限，剔除工具（degraded）")
            return
        hist.append(now)
        asyncio.create_task(self._restart(name))

    async def _restart(self, name: str) -> None:
        async with self._pool_lock:
            if name not in self._expected:
                return
            self._conns.pop(name, None)
            async with SessionLocal() as db:
                row = await db.scalar(select(MCPServer).where(MCPServer.name == name))
                if row is None:
                    return
                row.restart_count += 1
                await db.commit()
            await self._connect(row)
        await self._rebuild_tools()

    async def _set_status(self, name: str, status: str, *,
                          last_error: str | None = None,
                          tools_cache: list | None = None) -> None:
        async with SessionLocal() as db:
            row = await db.scalar(select(MCPServer).where(MCPServer.name == name))
            if row is None:
                return
            row.status = status
            if last_error is not None:
                row.last_error = last_error
            if tools_cache is not None:
                row.tools_cache = tools_cache
            if status == "connected":
                row.last_error = None
            await db.commit()

    async def _rebuild_tools(self) -> None:
        """合并所有在线 Server 的工具并失效图缓存（工具集变更需重建图）。"""
        tools: list[Any] = []
        risks: dict[str, str] = {}
        async with SessionLocal() as db:
            rows = (await db.execute(select(MCPServer))).scalars().all()
            overrides = {r.name: r.risk_overrides for r in rows}
        for name, conn in self._conns.items():
            if not conn.alive:
                continue
            try:
                infos = await conn.list_tools()
            except Exception:
                continue  # 故障剔除：该 Server 的工具不进入本轮工具集
            tools.extend(to_langchain_tools(conn, infos))
            for i in infos:
                risks[f"{name}__{i.name}"] = classify(i.name, overrides.get(name))
        self.tools = tools
        self.tool_risks = risks
        self._graph_cache.clear()

    # ---------------- 图与用户 ----------------

    def get_graph(self, model_id: str | None) -> Any:
        key = model_id or "__default__"
        if key not in self._graph_cache:
            self._graph_cache[key] = build_graph(self.tools, self.saver, model_id,
                                                 self.tool_risks)
        return self._graph_cache[key]

    async def ensure_user(self) -> str:
        """M1 单用户：固定一个本地用户。"""
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "local@agentflow.dev"))
            if user is None:
                user = User(email="local@agentflow.dev", display_name="本地用户",
                            default_model=settings.default_model)
                db.add(user)
                await db.commit()
            return user.id

    async def _sweep_expired_approvals(self) -> None:
        """后台巡检：waiting_approval 超过 approval_timeout_sec 的 run 注入默认拒绝。"""
        while True:
            try:
                await asyncio.sleep(30)
                cutoff = datetime.now(UTC) - timedelta(
                    seconds=settings.approval_timeout_sec)
                async with SessionLocal() as db:
                    rows = (await db.execute(
                        select(Run).where(Run.status == "waiting_approval",
                                          Run.finished_at.is_not(None),
                                          Run.finished_at < cutoff)
                    )).scalars().all()
                    expired = [(r.id, r.session_id) for r in rows]
                for run_id, session_id in expired:
                    print(f"[hitl-sweeper] run {run_id} 确认超时，自动拒绝")
                    async for _ in stream_resume(session_id, run_id, "reject", None,
                                                 "确认超时，系统自动拒绝", timeout=True):
                        pass  # 消费流完成恢复与落库
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[hitl-sweeper] 巡检异常（继续）: {e}")


runtime = Runtime()


# ---------------------------------------------------------------- SSE 转译

#: Harness Phase 2：为这些图节点建立 OTel span（节点名即 Runnable 名）
_NODE_SPANS = {"router", "guard", "agent", "gate", "authority_gate", "wait_approval",
               "escalate", "wrap_up", "tools"}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _hist_denied(snap: Any) -> int:
    """历史（跨轮）中被 Harness 拒绝的 ToolMessage 数（invariants no_denied 用）。"""
    with contextlib.suppress(Exception):
        return sum(1 for m in (snap.values.get("messages") or [])
                   if isinstance(m, ToolMessage)
                   and str(m.content).startswith(DENIED_PREFIX))
    return 0


async def _stream_run(run_id: str, session_id: str, model_id: str | None,
                      inputs: Any, *,
                      final_stop_reason: str | None = None,
                      contract: TaskContract | None = None) -> AsyncIterator[str]:
    """共享核心：驱动图执行 -> SSE 事件转译 -> 消息/工具落库 -> interrupt 检测。

    inputs 可以是新的 {"messages": ...}（首发）或 Command(resume=...)（恢复）。

    Harness Phase 1：
    - token 预算强制（contract.budgets.max_tokens）：超限干净终止（stop_reason=budget_exceeded），
      检查点已逐步持久化，天然可续（docs/02 ADR-D10）
    - 拒绝识别：本轮新增、以 DENIED_PREFIX 开头的 ToolMessage 下发 status=denied 的 tool_end
    - 完成门控（contract.gates_enabled）：自然结束时代码评估，未通过 stop_reason=gate_blocked
    """
    from langgraph.types import (
        Command,
    )

    text_parts: list[str] = []
    tool_records: dict[str, dict] = {}   # call_id -> 记录
    stop_reason = "end_turn"
    interrupt_payload: dict | None = None
    denied_msgs: list = []              # 本轮流新产生的 Harness 拒绝 ToolMessage
    denied_names: dict[str, str] = {}   # call_id -> 工具限定名
    gate_result = None
    budget_exceeded = False
    model_fallbacks: dict = {}
    wf_span = None
    node_spans: dict[str, Any] = {}     # event run_id -> 节点 span
    tool_spans: dict[str, Any] = {}     # event run_id -> 工具 span
    chat_spans: dict[str, Any] = {}     # event run_id -> 模型 span
    chat_tokens: dict[str, dict] = {}   # event run_id -> {input_tokens, output_tokens}
    budget_token = None
    emitted_channel = False
    escalations_emitted = 0
    budget_events_emitted = 0
    wrap_up_emitted = False
    current_channel = DEFAULT_CHANNEL
    final_escalations: list = []
    final_wrap_up = False

    # Harness Phase 2：从既有 usage 播种（resume 后 token 用量与预算累计）
    prev_usage: dict = {}
    with contextlib.suppress(Exception):
        async with SessionLocal() as db:
            _row = await db.get(Run, run_id)
            prev_usage = dict(_row.usage or {}) if _row else {}
    usage = {"input_tokens": int(prev_usage.get("input_tokens", 0)),
             "output_tokens": int(prev_usage.get("output_tokens", 0))}
    # Harness Phase 2：per-run 预算记账器（contextvar 注入图节点，供 agent 主动收敛）
    tracker = BudgetTracker(run_id, contract.budgets if contract else Budgets(),
                            tokens_used=usage["input_tokens"] + usage["output_tokens"])
    budget_token = current_budget.set(tracker)

    runtime._active_runs[run_id] = asyncio.current_task()
    try:
        graph = runtime.get_graph(model_id)
        # 通道路由：recursion_limit 数节点访问次数，按 calc+deep 步数上限动态计算
        cfg = {"configurable": {"thread_id": session_id},
               "recursion_limit": graph_recursion_limit()}
        # Harness Phase 2：workflow 根 span（节点/工具/模型 span 以其为 parent）
        wf_span = start_span("invoke_workflow", {
            RUN_ID: run_id, SESSION_ID: session_id,
            GEN_AI_CONVERSATION_ID: session_id, GEN_AI_AGENT_NAME: "agentflow",
            GEN_AI_OPERATION_NAME: "invoke_agent",
            GEN_AI_REQUEST_MODEL: model_id or settings.default_model,
        })
        # 记录流前消息 id 集合，用于识别本轮新增的拒绝 ToolMessage
        prev_ids: set[str] = set()
        prev_ctx_count = 0
        from langgraph.types import Command

        is_resume = isinstance(inputs, Command)
        try:
            snap0 = await graph.aget_state(cfg)
            prev_ids = {m.id for m in (snap0.values.get("messages") or [])
                        if getattr(m, "id", None)}
            prev_ctx_count = len(snap0.values.get("context_events") or [])
            if is_resume:
                # 恢复不回退通道：沿用 checkpoint 中的 channel / escalations
                prev_ch = snap0.values.get("channel")
                if prev_ch:
                    emitted_channel = True
                    current_channel = prev_ch
                    yield _sse("channel_selected", {
                        "channel": prev_ch, "reason": "resume", "decision": "resume",
                        "locked": bool(snap0.values.get("channel_locked")),
                        "clarify": bool(snap0.values.get("clarify"))})
                escalations_emitted = len(snap0.values.get("escalations") or [])
                budget_events_emitted = len(snap0.values.get("budget_events") or [])
        except Exception:  # noqa: BLE001, S110 — 快照读取失败仅影响 denied 识别，不阻断主流
            pass
        stream = graph.astream_events(inputs, cfg, version="v2")
        async for ev in stream:
            kind = ev["event"]
            if kind == "on_chain_start" and ev.get("name") in _NODE_SPANS:
                node_spans[ev["run_id"]] = start_span(
                    f"node:{ev['name']}",
                    {NODE_NAME: ev["name"], RUN_ID: run_id, SESSION_ID: session_id},
                    parent=wf_span)
            elif kind == "on_chain_end" and ev.get("name") in _NODE_SPANS:
                end_span(node_spans.pop(ev["run_id"], None))
                node = ev.get("name")
                out = (ev.get("data") or {}).get("output")
                if isinstance(out, dict):
                    # 通道选择：router 节点输出判定结果
                    if node == "router" and not emitted_channel:
                        rd = out.get("route_decision") or {}
                        current_channel = out.get("channel") or current_channel
                        emitted_channel = True
                        yield _sse("channel_selected", {
                            "channel": current_channel,
                            "reason": rd.get("reason"),
                            "decision": rd.get("decision"),
                            "locked": bool(out.get("channel_locked")),
                            "clarify": bool(out.get("clarify")),
                        })
                    # 升级记录：escalate 节点只追加，按已发送条数增量下发
                    esc = out.get("escalations")
                    if isinstance(esc, list) and len(esc) > escalations_emitted:
                        for rec in esc[escalations_emitted:]:
                            yield _sse("escalated", rec)
                        escalations_emitted = len(esc)
                        current_channel = esc[-1].get("to") or current_channel
                    # 预算耗尽：guard / authority_gate 累积的 budget_events
                    be = out.get("budget_events")
                    if isinstance(be, list) and len(be) > budget_events_emitted:
                        for rec in be[budget_events_emitted:]:
                            yield _sse("budget_exhausted",
                                       {**rec, "channel": current_channel})
                        budget_events_emitted = len(be)
                    # 到顶收尾：清空工具，再要一次纯文字作答
                    if out.get("wrap_up") and not wrap_up_emitted:
                        wrap_up_emitted = True
                        reason = out.get("escalate_reason") or (be[-1].get("reason") if be else None)
                        yield _sse("wrap_up", {"channel": current_channel,
                                               "reason": reason})
                if node == "tools":
                    yield _sse("state", {"node": "tools", "status": "running"})
            elif kind == "on_chat_model_start":
                chat_spans[ev["run_id"]] = start_span(
                    "chat_model",
                    {GEN_AI_OPERATION_NAME: "chat",
                     GEN_AI_REQUEST_MODEL: ev.get("name", model_id or ""),
                     RUN_ID: run_id, SESSION_ID: session_id},
                    parent=wf_span)
                chat_tokens[ev["run_id"]] = {"input_tokens": 0, "output_tokens": 0}
                yield _sse("state", {"node": "agent", "status": "running"})
            elif kind == "on_chat_model_end":
                ct = chat_tokens.pop(ev["run_id"], None)
                end_span(chat_spans.pop(ev["run_id"], None), attributes={
                    GEN_AI_USAGE_INPUT_TOKENS: ct.get("input_tokens") if ct else None,
                    GEN_AI_USAGE_OUTPUT_TOKENS: ct.get("output_tokens") if ct else None,
                })
            elif kind == "on_chat_model_stream":
                chunk = ev["data"]["chunk"]
                md = ev.get("metadata") or {}
                # 只转发 agent 节点的文本增量（router 结构化判定与工具参数流不转发）
                if isinstance(chunk.content, str) and chunk.content \
                        and md.get("langgraph_node") in (None, "agent"):
                    text_parts.append(chunk.content)
                    yield _sse("token", {"text": chunk.content})
                meta = getattr(chunk, "usage_metadata", None)
                if meta:
                    # 每次模型调用结束时附带 usage，按调用累计
                    it = int(meta.get("input_tokens", 0) or 0)
                    ot = int(meta.get("output_tokens", 0) or 0)
                    usage["input_tokens"] += it
                    usage["output_tokens"] += ot
                    tracker.record_tokens(input_tokens=it, output_tokens=ot)
                    ct = chat_tokens.get(ev["run_id"])
                    if ct is not None:
                        ct["input_tokens"] += it
                        ct["output_tokens"] += ot
                # Harness：token 预算强制（tracker 记账，超限干净终止，docs/02 ADR-D10）
                if tracker.token_exhausted() and not budget_exceeded:
                    budget_exceeded = True
                    stop_reason = "budget_exceeded"
                    yield _sse("error", {
                        "code": "budget_exceeded",
                        "message": (f"Token 预算耗尽（已用 {usage['input_tokens'] + usage['output_tokens']}"
                                    f" / 上限 {contract.budgets.max_tokens}），已保存检查点"),
                        "retryable": False})
                    break
            elif kind == "on_tool_start":
                data = ev["data"]
                call_id = str(ev.get("run_id", f"tc_{uuid.uuid4().hex[:12]}"))
                tool_records[call_id] = {
                    "tool": data.get("name", ev.get("name", "")),
                    "args": data.get("input") or {},
                    "t0": time.monotonic(),
                }
                tool_spans[ev["run_id"]] = start_span(
                    f"tool:{tool_records[call_id]['tool']}",
                    {GEN_AI_OPERATION_NAME: "execute_tool",
                     GEN_AI_TOOL_NAME: tool_records[call_id]["tool"],
                     RUN_ID: run_id, SESSION_ID: session_id},
                    parent=wf_span)
                yield _sse("tool_start", {"call_id": call_id,
                                          "tool": tool_records[call_id]["tool"],
                                          "args": tool_records[call_id]["args"]})
            elif kind == "on_tool_end":
                call_id = str(ev.get("run_id", ""))
                rec = tool_records.get(call_id, {"tool": ev.get("name", ""), "args": {}, "t0": time.monotonic()})
                raw = ev["data"].get("output", "")
                text = str(raw)
                status = "error" if text.startswith("ERROR") else "ok"
                dur = int((time.monotonic() - rec["t0"]) * 1000)
                rec.update(result=text, status=status, duration_ms=dur)
                tool_records[call_id] = rec
                end_span(tool_spans.pop(ev["run_id"], None), ok=(status == "ok"),
                         attributes={GEN_AI_TOOL_NAME: rec["tool"]})
                yield _sse("tool_end", {"call_id": call_id, "status": status,
                                       "duration_ms": dur,
                                       "result_preview": text[:2000]})

        if budget_exceeded:
            # 预算超限 break 后显式关闭事件流，及时取消图执行（检查点已持久化）
            with contextlib.suppress(Exception):
                await stream.aclose()

        # 状态快照：fallbacks / interrupt / denied / 门控评估共用
        snap = None
        with contextlib.suppress(Exception):
            snap = await graph.aget_state(cfg)
        if snap is not None:
            model_fallbacks = snap.values.get("model_fallbacks") or {}
            # 通道路由：最终通道与升级记录（升级只追加，不回退）
            current_channel = snap.values.get("channel") or current_channel
            final_escalations = list(snap.values.get("escalations") or [])
            # wrap_up 可能由节点写入 checkpoint 但事件未及下发（如流被截断）
            final_wrap_up = bool(snap.values.get("wrap_up"))

        # Harness Phase 3.3：上下文治理事件（agent 节点累积）-> SSE context_compressed
        if snap is not None:
            ctx_events = snap.values.get("context_events") or []
            for ce in ctx_events[prev_ctx_count:]:
                yield _sse("context_compressed", ce)

        # 流自然结束：检测是否停在 interrupt（astream_events 不抛 GraphInterrupt，
        # 中断表现为流结束 + aget_state 的 tasks 带 interrupts）
        if stop_reason == "end_turn" and snap is not None:
            for t in snap.tasks:
                for i in (getattr(t, "interrupts", None) or []):
                    interrupt_payload = i.value
                    break
                if interrupt_payload:
                    break
            if interrupt_payload:
                stop_reason = "waiting_approval"
            elif snap.values.get("loop_detected"):
                # Harness Phase 3.2：连续重复调用被强制终止（防失控循环）
                stop_reason = "loop_detected"
            else:
                # Harness：识别本轮新增的拒绝 ToolMessage -> denied tool_end + 落库
                new_msgs = [m for m in (snap.values.get("messages") or [])
                           if getattr(m, "id", None) not in prev_ids]
                denied_names = {c["id"]: c["name"] for m in new_msgs
                                if isinstance(m, AIMessage)
                                for c in (getattr(m, "tool_calls", None) or [])}
                denied_msgs = [m for m in new_msgs if isinstance(m, ToolMessage)
                               and str(m.content).startswith(DENIED_PREFIX)]
                # Harness：完成门控（自然结束时由确定性代码评估，非模型自评）
                if contract and contract.gates_enabled:
                    # 交付物正文（write_report 的 markdown）：报告出处写在文件里，
                    # 由 report_cites_queries 校验，不要求对话答复重复引用
                    deliverable = "\n".join(
                        str((r.get("args") or {}).get("markdown", ""))
                        for r in tool_records.values()
                        if str(r.get("tool", "")).endswith("write_report")
                        and isinstance(r.get("args"), dict))
                    gate_result = evaluate_gates(
                        output="".join(text_parts),
                        tool_total=len(tool_records),
                        tool_failed=sum(1 for r in tool_records.values()
                                        if r.get("status") != "ok"),
                        tokens_used=usage["input_tokens"] + usage["output_tokens"],
                        contract=contract,
                        # Harness Phase 3.4：契约 invariants 消费（含跨轮历史拒绝计数）
                        denied_total=len(denied_msgs) + _hist_denied(snap),
                        succeeded_tools=[r["tool"] for r in tool_records.values()
                                         if r.get("status") == "ok"],
                        # 通道路由：report_cites_queries 等 invariant 需要工具输出做溯源
                        tool_outputs=[r.get("result", "") for r in tool_records.values()],
                        deliverable=deliverable)
                    if not gate_result.passed:
                        stop_reason = "gate_blocked"
    except asyncio.CancelledError:
        stop_reason = "interrupted_stop"
    except Exception as e:
        import traceback
        traceback.print_exc()
        stop_reason = "error"
        yield _sse("error", {"code": "run_failed", "message": str(e)[:500] or repr(e)[:500], "retryable": True})
    finally:
        runtime._active_runs.pop(run_id, None)
        # Harness Phase 2：清理 contextvar 预算并兜底关闭未结束的 span
        if budget_token is not None:
            with contextlib.suppress(Exception):
                current_budget.reset(budget_token)
        for sp in [*node_spans.values(), *tool_spans.values(), *chat_spans.values()]:
            end_span(sp, ok=False)
        node_spans.clear()
        tool_spans.clear()
        chat_spans.clear()
        end_span(wf_span, ok=(stop_reason != "error"), attributes={
            "agentflow.stop_reason": stop_reason,
            GEN_AI_USAGE_INPUT_TOKENS: usage["input_tokens"],
            GEN_AI_USAGE_OUTPUT_TOKENS: usage["output_tokens"],
            FALLBACK_USED: bool(model_fallbacks.get("fallbacks")),
            "agentflow.budget.exceeded": budget_exceeded,
        })
        # 落库：assistant 消息（文本 + 工具调用）与 tool_calls 表
        assistant_text = "".join(text_parts)
        tc_list = [
            {"call_id": cid, "tool": r["tool"], "args": r["args"],
             "result": r.get("result", ""), "status": r.get("status", "ok"),
             "duration_ms": r.get("duration_ms", 0)}
            for cid, r in tool_records.items()
        ]
        async with SessionLocal() as db:
            if assistant_text or tc_list:
                db.add(Message(session_id=session_id, run_id=run_id, role="assistant",
                               content=assistant_text, tool_calls=tc_list or None,
                               usage=usage))
            for cid, r in tool_records.items():
                db.add(ToolCall(id=cid[:40], run_id=run_id, session_id=session_id,
                                server_id=r["tool"].split("__", 1)[0] if "__" in r["tool"] else None,
                                tool_name=r["tool"], args=r["args"],
                                result={"text": r.get("result", "")[:8000]},
                                status=r.get("status", "ok"),
                                risk_level=runtime.tool_risks.get(r["tool"], "low"),
                                duration_ms=r.get("duration_ms", 0)))
            # Harness：拒绝调用落库（status=denied，含真实风险等级）
            for m in denied_msgs:
                name = denied_names.get(m.tool_call_id, "unknown")
                db.add(ToolCall(id=m.tool_call_id[:40], run_id=run_id,
                                session_id=session_id,
                                server_id=name.split("__", 1)[0] if "__" in name else None,
                                tool_name=name, args=None,
                                result={"text": str(m.content)[:8000]},
                                status="denied",
                                risk_level=runtime.tool_risks.get(name, "low"),
                                duration_ms=0))
            run = await db.get(Run, run_id)
            if run:
                if stop_reason == "waiting_approval":
                    run.status = "waiting_approval"
                    run.stop_reason = stop_reason
                    run.usage = usage
                    run.finished_at = datetime.now(UTC)  # 本轮流已结束，作为超时起点
                else:
                    run.status = "finished" if stop_reason != "error" else "error"
                    run.stop_reason = final_stop_reason or stop_reason
                    run.usage = usage
                    run.finished_at = datetime.now(UTC)
            await db.commit()

    if interrupt_payload:
        yield _sse("interrupt", {**interrupt_payload, "run_id": run_id,
                                 "session_id": session_id})

    # Harness Phase 4：任务成功完成（门控通过）才沉淀长期记忆，避免污染
    if stop_reason == "end_turn" and assistant_text:
        with contextlib.suppress(Exception):
            from app.services.memory import store_memory

            user_id = await runtime.ensure_user()
            await store_memory(user_id, session_id, assistant_text)

    for m in denied_msgs:
        # 携带工具名：被拒调用不进入 tools 节点、无对应 tool_start，前端据此合成卡片
        yield _sse("tool_end", {"call_id": m.tool_call_id, "status": "denied",
                                "tool": denied_names.get(m.tool_call_id, "unknown"),
                                "duration_ms": 0,
                                "result_preview": str(m.content)[:2000]})

    # Harness Phase 2：下发模型降级事件（Policy fallback chain）
    for fb in (model_fallbacks.get("fallbacks") or []):
        yield _sse("fallback", fb)

    fin = {"run_id": run_id, "usage": usage, "stop_reason": stop_reason,
           "model_used": model_fallbacks.get("model_used"),
           "fallback_count": len(model_fallbacks.get("fallbacks") or []),
           "channel": current_channel,
           "escalations": final_escalations,
           "wrap_up": wrap_up_emitted or final_wrap_up,
           "budget": tracker.snapshot(tool_calls_used=len(tool_records))}
    if gate_result is not None:
        fin["gate_result"] = gate_result.model_dump()
    if denied_msgs:
        fin["denied_count"] = len(denied_msgs)
    yield _sse("run_finished", fin)


async def stream_chat(session_id: str, content: str, model_id: str | None,
                      contract: TaskContract | None = None,
                      channel: str | None = None) -> AsyncIterator[str]:
    """执行一次 run（首发消息），产出 SSE 字符串流。

    Harness：契约缺省用 TaskContract.default()（行为与改造前一致）；
    契约随 Run 落库、经 state 注入图（随 checkpoint 持久化，中断恢复后仍生效）。
    channel：显式指定通道（fast|calc|deep）。给定即锁定，不再由 router 判定/升级。
    """
    contract = contract or TaskContract.default()
    run_id = f"run_{uuid.uuid4().hex[:20]}"
    await runtime.ensure_user()

    # 落库：run + 用户消息
    async with SessionLocal() as db:
        sess = await db.get(ChatSession, session_id)
        if sess is None:
            yield _sse("error", {"code": "session_not_found", "message": "会话不存在", "retryable": False})
            return
        # 模型优先级：请求显式指定 > 会话配置 > 全局默认
        model_id = model_id or sess.model
        db.add(Run(id=run_id, session_id=session_id, status="running",
                   task_contract=contract.model_dump()))
        await db.flush()  # 保证 runs 先于 messages 插入（Postgres 强制外键）
        db.add(Message(session_id=session_id, run_id=run_id, role="user", content=content))
        # 首条消息自动生成会话标题
        if sess.title == "新会话":
            sess.title = content[:30]
        await db.commit()

    yield _sse("run_started", {"run_id": run_id, "session_id": session_id,
                               "thread_id": session_id,
                               "channel": channel or None,
                               "task_contract": contract.summary()})

    # 通道路由：显式 channel 由 router 锁定；缺省按 DEFAULT_CHANNEL 起步再判定
    inputs = {"messages": [HumanMessage(content=content)], "tool_calls_count": 0,
              "task_contract": contract.model_dump(),
              "requested_channel": channel,
              "channel": channel or DEFAULT_CHANNEL,
              "channel_locked": False}
    async for chunk in _stream_run(run_id, session_id, model_id, inputs, contract=contract):
        yield chunk


async def stream_resume(session_id: str, run_id: str, decision: str,
                        edited_args: dict | None, reason: str | None, *,
                        timeout: bool = False) -> AsyncIterator[str]:
    """HITL 恢复：Command(resume=...) 从中断点继续 run（SSE）。

    timeout=True 表示巡检任务注入的默认拒绝（run.stop_reason 记 approval_timeout）。
    """
    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
        if run is None or run.session_id != session_id:
            yield _sse("error", {"code": "run_not_found", "message": "run 不存在", "retryable": False})
            return
        if run.status == "interrupt_expired":
            yield _sse("error", {"code": "interrupt_expired", "message": "确认已超时被自动拒绝", "retryable": False})
            return
        if run.status != "waiting_approval":
            yield _sse("error", {"code": "run_not_interrupted",
                                 "message": "run 当前不在等待确认状态", "retryable": False})
            return
        sess = await db.get(ChatSession, session_id)
        model_id = sess.model if sess else None
        # Harness：恢复时沿用该 run 落库的契约（中断前的限制在恢复后仍生效）
        contract = (TaskContract.model_validate(run.task_contract)
                    if run.task_contract else TaskContract.default())

    from langgraph.types import Command

    resume_val = {"decision": decision, "edited_args": edited_args, "reason": reason}
    inputs = Command(resume=resume_val)
    async for chunk in _stream_run(run_id, session_id, model_id, inputs,
                                   final_stop_reason="approval_timeout" if timeout else None,
                                   contract=contract):
        yield chunk


async def stop_run(run_id: str) -> bool:
    """中断运行中的 run（取消后台流式任务）。"""
    task = runtime._active_runs.get(run_id)
    if task is None:
        return False
    task.cancel()
    return True
