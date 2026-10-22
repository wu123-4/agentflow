"""MCP Server 管理 API（docs/05 §2.4）：注册/启停/删除/工具预览，变更触发热重载。"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.models import MCPServer
from app.services.runtime import runtime

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ServerCreateReq(BaseModel):
    name: str
    transport: str = "stdio"  # stdio | streamable_http
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    enabled: bool = True
    risk_overrides: dict[str, str] | None = None


class ServerPatchReq(BaseModel):
    enabled: bool | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    risk_overrides: dict[str, str] | None = None


def _out(s: MCPServer) -> dict:
    return {
        "id": s.id, "name": s.name, "transport": s.transport,
        "command": s.command, "args": s.args, "url": s.url,
        "env_keys": sorted((s.env or {}).keys()),  # 不回显密钥值
        "enabled": s.enabled, "risk_overrides": s.risk_overrides,
        "status": s.status, "last_error": s.last_error,
        "tools": s.tools_cache or [], "tools_count": len(s.tools_cache or []),
        "restart_count": s.restart_count,
        "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat(),
    }


def _validate(s: ServerCreateReq | ServerPatchReq, *, partial: bool = False) -> None:
    if not partial and s.transport not in ("stdio", "streamable_http"):
        raise HTTPException(422, detail={"code": "invalid_transport",
                                         "message": "transport 仅支持 stdio | streamable_http"})
    if not partial:
        if s.transport == "stdio" and not s.command:
            raise HTTPException(422, detail={"code": "invalid_config",
                                             "message": "stdio 传输必须提供 command"})
        if s.transport == "streamable_http" and not s.url:
            raise HTTPException(422, detail={"code": "invalid_config",
                                             "message": "streamable_http 传输必须提供 url"})
        if not _NAME_RE.match(s.name):
            raise HTTPException(422, detail={"code": "invalid_name",
                                             "message": "name 需匹配 ^[a-z0-9][a-z0-9_-]{0,63}$（用作工具前缀）"})


@router.get("/servers")
async def list_servers(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(MCPServer).order_by(MCPServer.created_at))).scalars().all()
    return {"items": [_out(s) for s in rows]}


@router.post("/servers")
async def create_server(req: ServerCreateReq, db: AsyncSession = Depends(get_db)) -> dict:
    _validate(req)
    dup = await db.scalar(select(MCPServer).where(MCPServer.name == req.name))
    if dup is not None:
        raise HTTPException(409, detail={"code": "server_name_exists",
                                         "message": f"名称 {req.name} 已存在（工具前缀冲突）"})
    s = MCPServer(**req.model_dump())
    db.add(s)
    await db.commit()
    await runtime.reload_from_db()  # 热插拔：立即建连
    await db.refresh(s)  # 连接池在独立会话更新了 status/tools，需刷新
    return _out(s)


@router.patch("/servers/{server_id}")
async def patch_server(server_id: str, req: ServerPatchReq,
                       db: AsyncSession = Depends(get_db)) -> dict:
    s = await db.get(MCPServer, server_id)
    if s is None:
        raise HTTPException(404, detail={"code": "server_not_found", "message": "Server 不存在"})
    data = req.model_dump(exclude_unset=True)
    if "risk_overrides" in data and data["risk_overrides"]:
        bad = {k: v for k, v in data["risk_overrides"].items()
               if v not in ("low", "medium", "high")}
        if bad:
            raise HTTPException(422, detail={"code": "invalid_risk",
                                             "message": f"非法风险值: {bad}"})
    for k, v in data.items():
        setattr(s, k, v)
    await db.commit()
    await runtime.reload_from_db()  # 启停/配置变更 -> 热重载
    await db.refresh(s)
    return _out(s)


@router.delete("/servers/{server_id}")
async def delete_server(server_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    s = await db.get(MCPServer, server_id)
    if s is None:
        raise HTTPException(404, detail={"code": "server_not_found", "message": "Server 不存在"})
    name = s.name
    await db.delete(s)
    await db.commit()
    await runtime.reload_from_db()  # 断连并剔除工具
    return {"ok": True, "name": name}


@router.get("/servers/{server_id}/tools")
async def server_tools(server_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    s = await db.get(MCPServer, server_id)
    if s is None:
        raise HTTPException(404, detail={"code": "server_not_found", "message": "Server 不存在"})
    conn = runtime._conns.get(s.name)
    if conn is not None and conn.alive:
        try:
            infos = await conn.list_tools()
            from app.agent.risk import classify
            tools = [{"name": i.name, "description": i.description,
                      "input_schema": i.input_schema,
                      "risk": classify(i.name, s.risk_overrides)} for i in infos]
            return {"status": "connected", "tools": tools}
        except Exception as e:
            return {"status": "degraded", "tools": s.tools_cache or [], "error": str(e)[:300]}
    return {"status": s.status, "tools": s.tools_cache or []}
