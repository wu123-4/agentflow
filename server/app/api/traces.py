"""观测调试 API：查询某 run 的 span waterfall（Harness Phase 2）。

span 来自内存缓冲（app/observability/telemetry.py），无需 Jaeger 即可查询；
配置 OTEL_EXPORTER_OTLP_ENDPOINT 时同一批 span 也会导出到 Jaeger。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.observability import recent_spans

router = APIRouter(prefix="/api/v1", tags=["observability"])


@router.get("/traces/{run_id}")
async def get_trace(run_id: str) -> dict:
    return {"run_id": run_id, "items": recent_spans(run_id)}
