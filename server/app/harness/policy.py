"""模型重试与降级策略（Harness Phase 2，Policy）。

策略声明在 Harness 层（而非工具/节点内部）：tenacity 指数退避重试 +
单模型重试耗尽后切换到备用模型（fallback chain）。

约束：
- 预算超限不在此处理（由 runtime 干净终止，docs/02 ADR-D10）。
- 流式语义：仅在**尚未产出任何 chunk**时允许重试/降级；已开始输出则直接抛出，
  沿用现有 error 语义（不重放已下发的 token，避免重复内容）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings


class ModelUnavailableError(RuntimeError):
    """模型不可用（非瞬态）：不重试，直接进入降级链下一模型。"""


class AllModelsFailed(RuntimeError):
    """降级链全部失败。"""


# 瞬态异常按类名匹配（避免强依赖各 SDK 的具体异常类）：仅这些会触发指数退避重试
_TRANSIENT_NAMES = {
    "TimeoutError", "ConnectError", "ConnectionError", "RateLimitError",
    "APITimeoutError", "APIConnectionError", "InternalServerError",
    "ServiceUnavailableError",
}


def _is_transient(exc: BaseException) -> bool:
    return type(exc).__name__ in _TRANSIENT_NAMES or isinstance(
        exc, (TimeoutError, ConnectionError))


async def astream_with_policy(
    chain: list[tuple[str, Any]],
    messages: list,
    *,
    result: dict | None = None,
    fallbacks: list[dict] | None = None,
) -> AsyncIterator[Any]:
    """按 chain 顺序尝试模型流并产出 chunk。

    chain: [(model_id, model), ...]，首个为 primary，其余为 fallback。
    result: 输出参数，成功后写入 {"model_used": model_id}。
    fallbacks: 输出参数，记录每次降级 {"from", "to", "error"}。
    """
    last_exc: BaseException | None = None
    for idx, (mid, model) in enumerate(chain):
        state = {"produced": False}

        def _should_retry(exc: BaseException, *, _state: dict = state) -> bool:
            return (not _state["produced"]) and _is_transient(exc)

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(settings.policy_max_attempts),
                wait=wait_exponential(multiplier=1, min=settings.policy_backoff_min_sec,
                                      max=settings.policy_backoff_max_sec),
                retry=retry_if_exception(_should_retry),
                reraise=True,
            ):
                with attempt:
                    async for chunk in model.astream(messages):
                        state["produced"] = True
                        yield chunk
            # 成功
            if result is not None:
                result["model_used"] = mid
            return
        except Exception as e:  # noqa: BLE001 — 逐模型降级，最终统一收口
            last_exc = e
            if state["produced"]:
                # 已流出内容：不重试/降级，交上层按 error 处理
                raise
            if fallbacks is not None and idx + 1 < len(chain):
                fallbacks.append({"from": mid, "to": chain[idx + 1][0], "error": str(e)[:200]})
            continue
    raise AllModelsFailed(f"所有模型均不可用：{last_exc}") from last_exc