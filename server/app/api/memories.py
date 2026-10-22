"""长期记忆 API（Harness Phase 4）：查看/检索某用户的记忆条目（调试与验收用）。"""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import Memory
from app.services.runtime import runtime

router = APIRouter(prefix="/api/v1", tags=["memory"])


@router.get("/memories")
async def list_memories(session_id: str | None = None, q: str | None = None,
                       limit: int = 50) -> dict:
    """q 提供时走向量召回（score 排序）；否则按时间倒序列出。"""
    user_id = await runtime.ensure_user()
    if q and settings.memory_enabled:
        from app.services.memory import recall

        hits = await recall(user_id, q)
        return {"mode": "recall", "query": q,
                "items": [{**h, "session_id": None} for h in hits]}
    async with SessionLocal() as db:
        stmt = select(Memory).where(Memory.user_id == user_id)
        if session_id:
            stmt = stmt.where(Memory.session_id == session_id)
        rows = (await db.execute(
            stmt.order_by(Memory.created_at.desc()).limit(limit))).scalars().all()
    return {"mode": "list", "items": [
        {"id": m.id, "session_id": m.session_id, "kind": m.kind, "text": m.text,
         "created_at": m.created_at.isoformat()} for m in rows]}
