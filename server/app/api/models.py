"""模型注册表 API（Harness Phase 2，配置驱动）。

清单来源 app.config.MODEL_REGISTRY；provider 由 app.agent.graph.resolve_provider
按名前缀解析（与 build_model 同源）。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.agent.graph import resolve_provider
from app.config import MODEL_REGISTRY, settings

router = APIRouter(prefix="/api/v1", tags=["models"])


def _item(
    model_id: str,
    *,
    label: str | None = None,
    context_window: int | None = None,
    fallback: str | None = None,
) -> dict:
    return {
        "id": model_id,
        "label": label or model_id,
        "provider": resolve_provider(model_id),
        "context_window": context_window,
        "fallback": fallback,
        "is_default": model_id == settings.default_model,
    }


@router.get("/models")
async def list_models() -> dict:
    items = [
        _item(
            m["id"],
            label=m.get("label"),
            context_window=m.get("context_window"),
            fallback=m.get("fallback"),
        )
        for m in MODEL_REGISTRY
    ]
    if not any(i["is_default"] for i in items):
        items.insert(0, _item(settings.default_model))
    return {"items": items, "default": settings.default_model}
