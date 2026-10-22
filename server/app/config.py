"""全局配置：从项目根目录 .env 读取。"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # 数据库
    database_url: str = "postgresql+asyncpg://agentflow:agentflow@localhost:5432/agentflow"
    checkpoint_db_url: str = "postgresql://agentflow:agentflow@localhost:5432/agentflow"

    # 开发回退：无 Postgres/Docker 时用 SQLite + 内存检查点（数据存 server/dev.db）
    dev_sqlite: bool = False

    # 开发回退：无 LLM API Key 时用假模型脚本（验证 SSE/工具链路，仅开发用）
    dev_fake_model: bool = False

    # LLM
    openai_api_key: str = ""
    openai_base_url: str = ""
    anthropic_api_key: str = ""
    deepseek_api_key: str = ""
    default_model: str = "gpt-4o-mini"

    # MCP filesystem Server 可访问目录（相对 server/ 或绝对路径）
    mcp_filesystem_dir: str = "../workspace"

    # Agent 循环上限
    max_iterations: int = 8

    # HITL 确认超时（秒）：超时由巡检任务注入默认拒绝
    approval_timeout_sec: int = 600

    # 开发便利：启动时自动建表（生产使用 alembic upgrade head）
    auto_create_tables: bool = True

    # 可观测性（Harness Phase 2）：OTel tracing
    otel_enabled: bool = True
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "agentflow"
    otel_console_export: bool = False

    # 策略（Harness Phase 2）：重试与模型降级
    policy_max_attempts: int = 3
    policy_backoff_min_sec: float = 1.0
    policy_backoff_max_sec: float = 8.0

    # 恢复（Harness Phase 3）：工具层分级恢复
    recovery_max_attempts: int = 2
    recovery_backoff_min_sec: float = 0.2
    recovery_backoff_max_sec: float = 2.0
    # 写工具判定（工具限定名命中任一子串即视为写操作，需副作用对账）
    write_tool_patterns: list[str] = ["write", "create", "delete", "send"]

    # 循环检测（Harness Phase 3）：同一 tool+args 连续重复阈值
    loop_detect_threshold: int = 3

    # 上下文治理（Harness Phase 3）：Compaction / Reset 双策略
    context_compact_ratio: float = 0.7       # 估算 token 占模型 context_window 比例阈值
    context_max_messages: int = 40           # 消息条数阈值
    context_keep_recent: int = 6             # 治理后保留的最近消息条数
    context_strategy: str = "compaction"     # compaction | reset | auto
    context_state_path: str = "context_state.md"  # Reset 状态外化文件（相对工作区）

    # 长期记忆（Harness Phase 4）：pgvector（Postgres）/ 伪向量（SQLite dev 模式）
    memory_enabled: bool = True
    memory_top_k: int = 3
    embedding_model: str = ""                # 空 -> 确定性哈希伪向量（DEV 可验收）


settings = Settings()

# 模型注册表（Harness Phase 2）：id/label/context_window/fallback。
# fallback 为降级目标 id（None 表示不降级）。
MODEL_REGISTRY: list[dict] = [
    {
        "id": "deepseek-chat",
        "label": "DeepSeek Chat",
        "context_window": 65536,
        "fallback": "qwen-plus",
    },
    {
        "id": "deepseek-reasoner",
        "label": "DeepSeek Reasoner",
        "context_window": 65536,
        "fallback": "deepseek-chat",
    },
    {
        "id": "qwen-plus",
        "label": "通义千问 Plus",
        "context_window": 131072,
        "fallback": None,
    },
    {
        "id": "gpt-4o-mini",
        "label": "GPT-4o mini",
        "context_window": 128000,
        "fallback": None,
    },
    {
        "id": "claude-3-5-sonnet-latest",
        "label": "Claude 3.5 Sonnet",
        "context_window": 200000,
        "fallback": None,
    },
]


def registry_entry(model_id: str) -> dict | None:
    """按 id 查注册表条目。"""
    return next((m for m in MODEL_REGISTRY if m["id"] == model_id), None)


def fallback_ids(model_id: str) -> list[str]:
    """返回该模型的降级链（不含自身）；未配置则空列表（等价现状，不降级）。"""
    entry = registry_entry(model_id)
    fb = entry.get("fallback") if entry else None
    return [fb] if fb else []

if settings.dev_sqlite:
    settings.database_url = f"sqlite+aiosqlite:///{ROOT / 'server' / 'dev.db'}"

# 解析 MCP 目录为绝对路径（相对 server/ 目录）
_fs = Path(settings.mcp_filesystem_dir)
if not _fs.is_absolute():
    _fs = (ROOT / "server" / _fs).resolve()
MCP_FILESYSTEM_ABS_DIR = str(_fs)
