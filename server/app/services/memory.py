"""长期记忆（Harness Phase 4，docs/07 基线补齐）。

Embedding 可插拔：
- settings.embedding_model 为空（DEV/默认）：确定性哈希伪向量（token 桶 + L2 归一），
  无需外部 API，验收可复现。
- 配置了 embedding 端点：可扩展接真实模型（当前项目未接入外部 embedding，保留分支）。

检索双路径：
- Postgres + pgvector：vector 列 + 余弦距离 <=> 排序（启动时尽力 ALTER）。
- SQLite / pgvector 不可用：Python 端余弦相似度（演示规模下全量行扫描可接受）。
"""
from __future__ import annotations

import hashlib
import math
import re

from sqlalchemy import select, text

from app.config import settings
from app.db.base import SessionLocal, engine
from app.models import Memory

_EMBED_DIM = 64
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")


def embed(text: str) -> list[float]:
    """确定性伪向量：token -> sha1 桶 -> 计数，L2 归一。

    同义文本共享 token 越多、余弦相似度越高，足以支撑演示级语义召回。
    """
    vec = [0.0] * _EMBED_DIM
    tokens = _TOKEN_RE.findall((text or "").lower())
    for tok in tokens:
        h = hashlib.sha1(tok.encode("utf-8")).digest()
        idx = h[0] % _EMBED_DIM
        sign = 1.0 if h[1] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


async def store_memory(user_id: str, session_id: str | None, content: str, *,
                       kind: str = "summary") -> None:
    """沉淀一条记忆（开关关闭或文本过短时跳过）。"""
    if not settings.memory_enabled:
        return
    content = (content or "").strip()
    if len(content) < 8:
        return
    async with SessionLocal() as db:
        db.add(Memory(user_id=user_id, session_id=session_id, kind=kind,
                      text=content[:2000], vector=embed(content)))
        await db.commit()


async def recall(user_id: str, query: str, top_k: int | None = None) -> list[dict]:
    """向量召回 top-k：[{id, text, kind, score}]，相似度阈值过滤噪声。"""
    if not settings.memory_enabled or not query.strip():
        return []
    k = top_k or settings.memory_top_k
    qv = embed(query)
    # Postgres + pgvector 优先（SQL 端排序）
    if engine.dialect.name == "postgresql":
        try:
            async with SessionLocal() as db:
                rows = (await db.execute(text(
                    "SELECT id, text, kind, 1 - (vector <=> CAST(:q AS vector)) AS score "
                    "FROM memories WHERE user_id = :u "
                    "ORDER BY vector <=> CAST(:q AS vector) LIMIT :k"),
                    {"q": str(qv), "u": user_id, "k": k})).all()
            return [{"id": r[0], "text": r[1], "kind": r[2], "score": float(r[3])}
                    for r in rows if float(r[3]) >= 0.2]
        except Exception:  # noqa: BLE001 — 列未转 vector 等场景回退 Python 路径
            pass
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(Memory).where(Memory.user_id == user_id)
            .order_by(Memory.created_at.desc()).limit(200))).scalars().all()
    scored = [{"id": m.id, "text": m.text, "kind": m.kind,
               "score": _cosine(qv, m.vector or [])}
              for m in rows if m.vector]
    scored.sort(key=lambda r: r["score"], reverse=True)
    return [r for r in scored[:k] if r["score"] >= 0.2]


async def recall_prompt(query: str, top_k: int | None = None) -> str:
    """召回并组装为注入上下文的提示文本（M1 单用户；无命中返回空串）。"""
    from app.services.runtime import runtime

    user_id = await runtime.ensure_user()
    hits = await recall(user_id, query, top_k)
    if not hits:
        return ""
    lines = "\n".join(f"- {h['text'][:200]}（相关度 {h['score']:.2f}）" for h in hits)
    return ("【长期记忆（跨会话召回，仅供参考）】\n" + lines
            + "\n如与当前任务冲突，以用户最新指令为准。")


async def ensure_pgvector() -> None:
    """Postgres 下尽力启用 pgvector 并把 vector 列转为 VECTOR(dim)；失败静默回退。"""
    if engine.dialect.name != "postgresql":
        return
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text(
                f"ALTER TABLE memories ALTER COLUMN vector TYPE vector({_EMBED_DIM})"))
    except Exception as e:  # noqa: BLE001 — 扩展不可用不影响 JSON 向量路径
        print(f"[memory] pgvector 未启用（回退 Python 余弦）: {str(e)[:120]}")
