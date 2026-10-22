"""OpenTelemetry 观测（Harness Phase 2）。

双通道 span 导出：
- 内存缓冲（始终开启）：有界环形缓冲，按 run_id 归集，供调试端点与验收脚本断言，
  无需 Jaeger/Docker 也能全自动验收。
- OTLP→Jaeger：配置 OTEL_EXPORTER_OTLP_ENDPOINT 时启用（BatchSpanProcessor）；
  未配置时可选择控制台导出（OTEL_CONSOLE_EXPORT=true）。

属性遵循 OTel GenAI 语义约定（gen_ai.*）。
"""
from __future__ import annotations

import contextlib
import threading
from collections import deque
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, Status, StatusCode

from app.config import settings

# ---- OTel GenAI 语义约定属性键 ----
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
# ---- 项目自有属性 ----
RUN_ID = "agentflow.run.id"
SESSION_ID = "agentflow.session.id"
NODE_NAME = "agentflow.node"
FALLBACK_USED = "agentflow.fallback.used"

_SERVICE_VERSION = "0.2.0"
_MAX_SPANS_PER_RUN = 500


class InMemorySpanExporter(SpanExporter):
    """有界内存 span 导出器：按 run_id 归集，供调试端点/验收断言。"""

    def __init__(self) -> None:
        self._by_run: dict[str, deque[ReadableSpan]] = {}
        self._lock = threading.Lock()

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            for sp in spans:
                attrs = sp.attributes or {}
                run_id = str(attrs.get(RUN_ID, ""))
                buf = self._by_run.get(run_id)
                if buf is None:
                    buf = deque(maxlen=_MAX_SPANS_PER_RUN)
                    self._by_run[run_id] = buf
                buf.append(sp)
        return SpanExportResult.SUCCESS

    def spans_of(self, run_id: str) -> list[ReadableSpan]:
        with self._lock:
            return list(self._by_run.get(run_id, ()))

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


_mem_exporter = InMemorySpanExporter()
_provider: TracerProvider | None = None
_tracer: trace.Tracer | None = None


def _normalize_otlp_endpoint(endpoint: str) -> str:
    """OTLP/HTTP trace 端点：允许只配 base（http://host:4318）。"""
    ep = endpoint.rstrip("/")
    return ep if ep.endswith("/v1/traces") else f"{ep}/v1/traces"


def setup_telemetry() -> None:
    """初始化 TracerProvider（lifespan 启动时调用）。"""
    global _provider, _tracer
    if not settings.otel_enabled or _provider is not None:
        return
    resource = Resource.create({
        "service.name": settings.otel_service_name,
        "service.version": _SERVICE_VERSION,
    })
    provider = TracerProvider(resource=resource)
    # 内存缓冲：Simple 处理器保证 span 结束即可查询（验收确定性）
    provider.add_span_processor(SimpleSpanProcessor(_mem_exporter))
    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=_normalize_otlp_endpoint(
                settings.otel_exporter_otlp_endpoint))))
    elif settings.otel_console_export:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    # 已被其他组件设置过 provider 时复用现有（set_tracer_provider 只能设置一次）
    with contextlib.suppress(Exception):
        trace.set_tracer_provider(provider)
    _provider = provider
    _tracer = trace.get_tracer("agentflow")


def shutdown_telemetry() -> None:
    """关闭 TracerProvider（lifespan 退出时调用）。"""
    global _provider, _tracer
    if _provider is not None:
        _provider.shutdown()
    _provider = None
    _tracer = None


def enabled() -> bool:
    return _tracer is not None


def get_tracer() -> trace.Tracer:
    return _tracer if _tracer is not None else trace.get_tracer("agentflow")


def start_span(name: str, attributes: dict[str, Any] | None = None,
               parent: Span | None = None) -> Span | None:
    """开启一个 span；未启用观测时返回 None（调用方无需分支）。"""
    if _tracer is None:
        return None
    ctx = trace.set_span_in_context(parent) if parent is not None else None
    attrs = {k: v for k, v in (attributes or {}).items() if v is not None}
    return get_tracer().start_span(name, context=ctx, attributes=attrs)


def end_span(span: Span | None, *, ok: bool = True,
             attributes: dict[str, Any] | None = None) -> None:
    """结束 span：可附最终属性与错误状态。"""
    if span is None:
        return
    if attributes:
        clean = {k: v for k, v in attributes.items() if v is not None}
        if clean:
            span.set_attributes(clean)
    if not ok:
        span.set_status(Status(StatusCode.ERROR))
    span.end()


def recent_spans(run_id: str) -> list[dict]:
    """返回某 run 的 span waterfall（按开始时间升序）。"""
    spans = sorted(_mem_exporter.spans_of(run_id), key=lambda s: s.start_time or 0)
    out: list[dict] = []
    for s in spans:
        start_ns = s.start_time or 0
        end_ns = s.end_time or start_ns
        out.append({
            "name": s.name,
            "span_id": format(s.context.span_id, "016x") if s.context else None,
            "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
            "start_ms": start_ns // 1_000_000,
            "duration_ms": max(0, (end_ns - start_ns) // 1_000_000),
            "status": s.status.status_code.name if s.status else "UNSET",
            "attributes": dict(s.attributes or {}),
        })
    return out