"""Harness Phase 3：工具级分级恢复（docs/07 Phase 3）。

原则：恢复逻辑绝不向图抛异常——失败统一转为回喂文本，由模型决定下一步。

分级策略：
- 只读工具：瞬态异常（超时/连接/限流，按类名匹配）-> tenacity 指数退避重试；
  非瞬态异常 -> 不重试，直接失败回喂。
- 写工具（限定名命中 settings.write_tool_patterns）：瞬态失败后**先对账副作用**再决定——
  确认已写入 -> 视为成功不重试；确认未写入 -> 安全重试；无法对账 -> 回喂"不确定"消息，
  引导模型先核实再决定是否重试（避免盲目重复写）。

确定性故障注入（仅 DEV_FAKE_MODEL）：工具 args 含 "flaky <nonce>" 时首次抛 TimeoutError、
下次成功，使"工具超时 -> 退避重试成功"可被验收脚本确定性复现。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings
from app.harness.policy import _TRANSIENT_NAMES


def is_transient(exc: BaseException) -> bool:
    """瞬态异常判定（与模型 Policy 同源：按类名匹配，不依赖具体 SDK）。"""
    return type(exc).__name__ in _TRANSIENT_NAMES or isinstance(
        exc, (TimeoutError, ConnectionError))


def is_write_tool(name: str) -> bool:
    """写操作判定：工具限定名（小写）命中任一 write_tool_patterns 子串。"""
    n = name.lower()
    return any(p in n for p in settings.write_tool_patterns)


def call_hash(name: str, args: Any) -> str:
    """action+params 规范化哈希（循环检测用）。"""
    payload = json.dumps(args if args is not None else {}, sort_keys=True,
                         ensure_ascii=False, default=str)
    return hashlib.sha1(f"{name}|{payload}".encode("utf-8")).hexdigest()[:16]


# ---- dev 确定性故障注入 ----
_FLAKY_RE = re.compile(r"flaky\s+([0-9a-zA-Z]{4,32})")
_flaky_counters: dict[str, int] = {}


def _inject_flaky(name: str, args: Any) -> None:
    if not settings.dev_fake_model:
        return
    text = json.dumps(args or {}, ensure_ascii=False, default=str)
    m = _FLAKY_RE.search(text)
    if not m:
        return
    nonce = m.group(1)
    n = _flaky_counters.get(nonce, 0)
    _flaky_counters[nonce] = n + 1
    if n == 0:
        raise TimeoutError(f"模拟工具瞬态超时（DEV 注入）: {name}")


# ---- 写操作副作用对账 ----
def _reconcile(name: str, args: Any) -> tuple[str, str]:
    """返回 (verdict, text)：done=确认已写入 / absent=确认未写入 / unknown=无法判定。"""
    if "write" not in name.lower():
        return "unknown", ""
    path = None
    if isinstance(args, dict):
        for k in ("path", "file_path", "filename"):
            if isinstance(args.get(k), str):
                path = args[k]
                break
    if not path:
        return "unknown", ""
    try:
        from pathlib import Path

        p = Path(path)
        if p.exists():
            return "done", (f"对账结果：写副作用已发生（{p.name}，"
                            f"{p.stat().st_size} 字节），视为成功，未重复执行。")
        return "absent", ""
    except OSError:
        return "unknown", ""


async def _execute_readonly(tool: Any, args: Any, name: str) -> tuple[str, str]:
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(settings.recovery_max_attempts + 1),
            wait=wait_exponential(multiplier=1, min=settings.recovery_backoff_min_sec,
                                  max=settings.recovery_backoff_max_sec),
            retry=retry_if_exception(is_transient),
            reraise=True,
        ):
            with attempt:
                _inject_flaky(name, args)
                result = await tool.ainvoke(args)
                return str(result), "ok"
    except Exception as e:  # noqa: BLE001 — 统一收口为回喂文本
        return f"工具执行失败: {e}", "error"
    return f"工具执行失败: {name}", "error"  # pragma: no cover — 防御性兜底


async def _execute_write(tool: Any, args: Any, name: str) -> tuple[str, str]:
    """写工具：每次瞬态失败先对账，只有"确认未写入"才安全重试。"""
    attempts = settings.recovery_max_attempts + 1
    backoff = settings.recovery_backoff_min_sec
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            _inject_flaky(name, args)
            result = await tool.ainvoke(args)
            return str(result), "ok"
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if not is_transient(e):
                return f"工具执行失败: {e}", "error"
            verdict, text = _reconcile(name, args)
            if verdict == "done":
                return text, "ok"
            if verdict == "unknown":
                return ("ERROR: 写操作瞬态失败且无法对账副作用"
                        f"（{e}）。请先用只读方式核实是否已生效，再决定是否重试，"
                        "不要盲目重复写入。"), "error"
            # absent：确认未写入 -> 退避后安全重试
            if i < attempts - 1:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, settings.recovery_backoff_max_sec)
    return f"工具执行失败: {last_exc}", "error"


async def execute_with_recovery(tool: Any, args: Any, *,
                                tool_name: str | None = None) -> tuple[str, str]:
    """带分级恢复地执行一次工具调用，返回 (结果文本, 状态 ok|error)。"""
    name = tool_name or getattr(tool, "name", "")
    if is_write_tool(name):
        return await _execute_write(tool, args, name)
    return await _execute_readonly(tool, args, name)
