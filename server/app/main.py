"""FastAPI 应用入口。"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, mcp, memories, models, sessions, traces
from app.config import settings
from app.observability import setup_telemetry, shutdown_telemetry
from app.services.runtime import runtime


async def _ensure_columns() -> None:
    """幂等迁移：create_all 只建新表、不给已有表加列。

    runs.task_contract（Harness Phase 1）需 ALTER 补列：
    - PostgreSQL: ADD COLUMN IF NOT EXISTS
    - SQLite(dev.db 已存在时): ADD COLUMN，重复列报错吞掉
    """
    from sqlalchemy import text

    from app.db.base import engine
    if engine.dialect.name == "postgresql":
        stmts = ["ALTER TABLE runs ADD COLUMN IF NOT EXISTS task_contract JSON"]
    else:
        stmts = ["ALTER TABLE runs ADD COLUMN task_contract JSON"]
    for stmt in stmts:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
        except Exception as e:
            msg = str(e).lower()
            if "already exists" not in msg and "duplicate column" not in msg:
                raise


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_telemetry()
    if settings.auto_create_tables:
        from app.db.base import engine
        from app.models import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _ensure_columns()
    await runtime.setup()
    if settings.memory_enabled:
        from app.services.memory import ensure_pgvector

        await ensure_pgvector()
    yield
    await runtime.teardown()
    shutdown_telemetry()


app = FastAPI(title="AgentFlow API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(sessions.router)
app.include_router(mcp.router)
app.include_router(models.router)
app.include_router(traces.router)
app.include_router(memories.router)


@app.get("/api/v1/health")
async def health() -> dict:
    return {
        "status": "ok",
        "mcp_servers": {name: conn.alive for name, conn in runtime._conns.items()},
        "tools": [t.name for t in runtime.tools],
    }
