"""MCP 集成层：stdio 连接管理 + 把 MCP 工具适配为 LangChain BaseTool。

关键约束：MCP SDK 的 stdio_client 内部使用 anyio cancel scope，
必须在**同一个 asyncio task** 中进入与退出，否则报
"Attempted to exit cancel scope in a different task"。
因此每个 Server 连接由一个专属后台 task 持有会话，外部通过请求队列与之通信。
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class MCPToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class _Request:
    kind: str                      # "list_tools" | "call_tool"
    payload: dict[str, Any] = field(default_factory=dict)
    future: asyncio.Future = field(default=None)  # type: ignore[assignment]


class MCPServerConnection:
    """在专属后台 task 中管理一个 stdio MCP Server 会话。"""

    def __init__(self, server_id: str, command: str,
                 args: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        self.server_id = server_id
        self._params = StdioServerParameters(command=command, args=args or [], env=env)
        self._queue: asyncio.Queue[_Request | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._startup_error: BaseException | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.server_id}")
        await self._ready.wait()
        if self._startup_error:
            raise self._startup_error

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)  # 哨兵：退出循环
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(self._task, timeout=10)
        self._task = None

    async def _run(self) -> None:
        """后台 task：进入 stdio 上下文 -> 处理请求 -> 退出。全程同 task。"""
        try:
            async with stdio_client(self._params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._ready.set()
                    while True:
                        req = await self._queue.get()
                        if req is None:
                            return
                        try:
                            if req.kind == "list_tools":
                                resp = await session.list_tools()
                                req.future.set_result(resp)
                            elif req.kind == "call_tool":
                                resp = await session.call_tool(
                                    req.payload["name"], req.payload["args"])
                                req.future.set_result(resp)
                        except Exception as e:  # 单个请求失败不影响会话
                            if not req.future.done():
                                req.future.set_exception(e)
        except BaseException as e:
            self._startup_error = e
            if not self._ready.is_set():
                self._ready.set()
            raise

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def add_exit_callback(self, cb) -> None:
        """后台 task 退出（崩溃或正常停止）时回调，供连接池监督重启。"""
        if self._task is not None:
            self._task.add_done_callback(cb)

    async def _ask(self, kind: str, payload: dict | None = None,
                   timeout: float = 60) -> Any:
        if self._task is None or self._task.done():
            raise RuntimeError(f"MCP server {self.server_id} 未连接")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put(_Request(kind=kind, payload=payload or {}, future=fut))
        return await asyncio.wait_for(fut, timeout=timeout)

    async def list_tools(self) -> list[MCPToolInfo]:
        resp = await self._ask("list_tools")
        out: list[MCPToolInfo] = []
        for t in resp.tools:
            schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
            out.append(MCPToolInfo(t.name, t.description or "", dict(schema or {})))
        return out

    async def call_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """调用工具，返回 (文本结果, is_error)。"""
        result = await self._ask("call_tool", {"name": name, "args": args})
        parts: list[str] = []
        is_error = bool(getattr(result, "is_error", None) or getattr(result, "isError", False))
        for block in result.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
            else:
                parts.append(f"[{getattr(block, 'type', 'unknown')} 内容]")
        return "\n".join(parts), is_error


def _qualified(server_id: str, tool_name: str) -> str:
    return f"{server_id}__{tool_name}"


_TYPE_MAP = {"string": str, "number": float, "integer": int, "boolean": bool,
             "object": dict, "array": list}


def _schema_to_model(server_id: str, tool_name: str, schema: dict[str, Any]):
    """把 MCP JSON Schema 转为 Pydantic 模型，供 LangChain 正确校验与传参。"""
    from pydantic import Field, create_model

    props: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for key, spec in props.items():
        py_type = _TYPE_MAP.get(str(spec.get("type")), Any)
        desc = spec.get("description")
        if key in required:
            fields[key] = (py_type, Field(description=desc))
        else:
            fields[key] = (py_type | None, Field(default=None, description=desc))
    model_name = f"{server_id}_{tool_name}".replace("-", "_")
    return create_model(model_name, **fields)


def to_langchain_tools(conn: MCPServerConnection, infos: list[MCPToolInfo]) -> list[BaseTool]:
    """把某个 Server 的工具列表包装为 LangChain 工具（命名 {server}__{tool}）。"""

    def _make(info: MCPToolInfo) -> BaseTool:
        async def _run(**kwargs: Any) -> str:
            args = {k: v for k, v in kwargs.items() if v is not None}
            text, is_error = await conn.call_tool(info.name, args)
            return f"ERROR: {text}" if is_error else text

        return StructuredTool(
            name=_qualified(conn.server_id, info.name),
            description=info.description or info.name,
            coroutine=_run,
            args_schema=_schema_to_model(conn.server_id, info.name, info.input_schema),
        )

    return [_make(i) for i in infos]
