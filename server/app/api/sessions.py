"""会话与消息 CRUD API（契约见 docs/05-API设计.md）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.models import Message, Run, ToolCall, User
from app.models import Session as ChatSession
from app.services.runtime import runtime

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


class CreateSessionReq(BaseModel):
    title: str = "新会话"
    model: str | None = None


class PatchSessionReq(BaseModel):
    title: str | None = None
    model: str | None = None


def _sess_dict(s: ChatSession, message_count: int = 0) -> dict:
    return {
        "id": s.id, "title": s.title, "model": s.model, "status": s.status,
        "message_count": message_count,
        "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat(),
    }


@router.get("")
async def list_sessions(db: AsyncSession = Depends(get_db)) -> dict:
    user_id = await runtime.ensure_user()
    rows = await db.execute(
        select(ChatSession, func.count(Message.id))
        .outerjoin(Message, Message.session_id == ChatSession.id)
        .where(ChatSession.user_id == user_id, ChatSession.status == "active")
        .group_by(ChatSession.id)
        .order_by(ChatSession.updated_at.desc())
    )
    return {"items": [_sess_dict(s, c) for s, c in rows]}


@router.post("")
async def create_session(req: CreateSessionReq, db: AsyncSession = Depends(get_db)) -> dict:
    user_id = await runtime.ensure_user()
    s = ChatSession(user_id=user_id, title=req.title, model=req.model)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return _sess_dict(s)


@router.patch("/{session_id}")
async def patch_session(session_id: str, req: PatchSessionReq,
                        db: AsyncSession = Depends(get_db)) -> dict:
    s = await db.get(ChatSession, session_id)
    if s is None:
        raise HTTPException(404, detail={"code": "session_not_found", "message": "会话不存在"})
    if req.title is not None:
        s.title = req.title
    if req.model is not None:
        s.model = req.model
    await db.commit()
    await db.refresh(s)
    return _sess_dict(s)


@router.delete("/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    s = await db.get(ChatSession, session_id)
    if s is None:
        raise HTTPException(404, detail={"code": "session_not_found", "message": "会话不存在"})
    # 级联删除检查点（thread_id = session_id）
    if runtime.saver is not None:
        try:
            await runtime.saver.adelete_thread(session_id)
        except Exception:
            pass
    await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
    await db.commit()
    return {"ok": True}


@router.get("/{session_id}/messages")
async def list_messages(session_id: str, limit: int = 50, before: str | None = None,
                        db: AsyncSession = Depends(get_db)) -> dict:
    q = select(Message).where(Message.session_id == session_id)
    if before:
        anchor = await db.get(Message, before)
        if anchor:
            q = q.where(Message.created_at < anchor.created_at)
    rows = (await db.execute(q.order_by(Message.created_at.desc()).limit(limit))).scalars().all()
    items = list(reversed(rows))
    return {
        "items": [
            {"id": m.id, "role": m.role, "content": m.content,
             "tool_calls": m.tool_calls, "created_at": m.created_at.isoformat()}
            for m in items
        ]
    }


@router.get("/{session_id}/runs")
async def list_runs(session_id: str, limit: int = 50,
                    db: AsyncSession = Depends(get_db)) -> dict:
    """运行轨迹（Harness Phase 2）：该会话的 run 列表。

    聚合既有 runs / tool_calls 表（无新表）：usage 用量、stop_reason 终态、
    工具调用次数与起止时间，供前端展示"这一步在做什么、为什么停"。
    """
    rows = (await db.execute(
        select(Run, func.count(ToolCall.id))
        .outerjoin(ToolCall, ToolCall.run_id == Run.id)
        .where(Run.session_id == session_id)
        .group_by(Run.id)
        .order_by(Run.started_at.desc())
        .limit(limit)
    )).all()
    return {
        "items": [
            {"id": r.id, "status": r.status, "stop_reason": r.stop_reason,
             "usage": r.usage or {}, "tool_calls": tc_count,
             "task_contract": r.task_contract,
             "started_at": r.started_at.isoformat(),
             "finished_at": r.finished_at.isoformat() if r.finished_at else None}
            for r, tc_count in rows
        ]
    }
