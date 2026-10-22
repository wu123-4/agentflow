"""对话 API：POST /chat 与 POST /chat/resume 以 SSE 流式返回；POST /chat/{run_id}/stop 中断。"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.harness.contract import TaskContract
from app.services.runtime import (
    stop_run,
    stream_chat,
    stream_resume,
)

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatReq(BaseModel):
    session_id: str
    content: str
    model: str | None = None
    # Harness：任务契约（docs/01 FR-25）。缺省为默认契约（不收紧），非法字段 422
    task_contract: TaskContract | None = None
    # 通道路由：显式指定预算档位（非法值 422）；缺省由 router 判定
    channel: Literal["fast", "calc", "deep"] | None = None


class ResumeReq(BaseModel):
    session_id: str
    run_id: str
    interrupt_id: str | None = None
    decision: str  # approve | edit | reject
    edited_args: dict | None = None
    reason: str | None = None


def _sse_response(gen) -> StreamingResponse:
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/chat")
async def chat(req: ChatReq) -> StreamingResponse:
    return _sse_response(stream_chat(req.session_id, req.content, req.model,
                                     req.task_contract, req.channel))


@router.post("/chat/resume")
async def resume(req: ResumeReq) -> StreamingResponse:
    if req.decision not in ("approve", "edit", "reject"):
        return JSONResponse(status_code=422, content={"detail": {"code": "invalid_decision",
                                                                  "message": "decision 仅支持 approve|edit|reject"}})
    return _sse_response(stream_resume(req.session_id, req.run_id, req.decision,
                                       req.edited_args, req.reason))


@router.post("/chat/{run_id}/stop")
async def stop(run_id: str) -> dict:
    stopped = await stop_run(run_id)
    return {"ok": True, "stopped": stopped}
