"""SQLAlchemy ORM 模型（M1 子集：users / sessions / messages / runs / tool_calls）。"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("usr"))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(120), default="本地用户")
    default_model: Mapped[str] = mapped_column(String(120), default="gpt-4o-mini")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("sess"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255), default="新会话")
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    __table_args__ = (Index("ix_sessions_user_updated", "user_id", updated_at.desc()),)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("run"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(30), default="running")
    stop_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    task_contract: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # Harness：本次 run 的任务契约
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_runs_session_started", "session_id", started_at.desc()),)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("msg"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    role: Mapped[str] = mapped_column(String(20))  # user / assistant / tool / system
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_messages_session_created", "session_id", created_at),)


class MCPServer(Base):
    """MCP Server 注册表（M2）：持久化配置，Runtime 连接池按此动态建连。"""

    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("mcp"))
    name: Mapped[str] = mapped_column(String(120), unique=True)  # server_id，用于工具前缀
    transport: Mapped[str] = mapped_column(String(20), default="stdio")  # stdio | streamable_http
    command: Mapped[str | None] = mapped_column(String(255), nullable=True)
    args: Mapped[list | None] = mapped_column(JSON, nullable=True)
    env: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # M5 前明文存储（仅本地）
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)  # streamable_http 预留
    enabled: Mapped[bool] = mapped_column(default=True)
    risk_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {tool: low|medium|high}
    status: Mapped[str] = mapped_column(String(20), default="disconnected")  # connected/degraded/disconnected
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tools_cache: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{name,description,risk}]
    restart_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("tc"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    server_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(160))
    args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    risk_level: Mapped[str] = mapped_column(String(10), default="low")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_tool_calls_run", "run_id"),)


class Memory(Base):
    """长期记忆（Harness Phase 4）：跨会话事实沉淀 + 向量召回。

    vector 列在 Postgres+pgvector 下经运行时 ALTER 为 VECTOR(dim)，
    SQLite（dev）保持 JSON 并在 Python 端做余弦相似度。
    """

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("mem"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="summary")  # summary / fact
    text: Mapped[str] = mapped_column(Text)
    vector: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_memories_user_created", "user_id", created_at.desc()),)
