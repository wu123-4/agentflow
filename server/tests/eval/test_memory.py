"""Phase 4 评估集 · 长期记忆（5）：沉淀 / 列表 / 向量召回 / 跨会话召回注入 / 开关分支。"""
from __future__ import annotations

import uuid


def test_memory_store_after_run(client):
    """任务成功（end_turn 且有文本）后应沉淀一条记忆。"""
    tag = uuid.uuid4().hex[:8]
    sid = client.mk_session("eval mem store")
    evs = client.chat(f"你好 记忆标记{tag}", session_id=sid)
    fin = client.score("memory:store", evs, expect="end_turn")
    assert fin.get("stop_reason") == "end_turn", fin
    items = client.get("/memories", session_id=sid)["items"]
    assert any(tag in it["text"] or "AgentFlow" in it["text"] for it in items), items


def test_memory_list_mode(client):
    """无 q 时按时间倒序列出（mode=list）。"""
    sid = client.mk_session("eval mem list")
    evs = client.chat("你好", session_id=sid)
    client.score("memory:list", evs, expect="end_turn")
    data = client.get("/memories", session_id=sid)
    assert data["mode"] == "list"
    assert all({"id", "text", "kind", "created_at"} <= set(it) for it in data["items"])


def test_memory_recall_vector(client):
    """q 走向量召回（mode=recall），共享 token 的记忆应命中且带 score。"""
    tag = uuid.uuid4().hex[:8]
    sid = client.mk_session("eval mem recall")
    evs = client.chat(f"总结 readme 文件 关键词{tag}", session_id=sid)
    client.score("memory:recall", evs, expect="end_turn")
    data = client.get("/memories", q=f"总结 readme 文件 关键词{tag}")
    assert data["mode"] == "recall"
    assert data["items"], data
    assert all(it["score"] >= 0.2 for it in data["items"])


def test_memory_cross_session_injection(client):
    """跨会话：先沉淀含独特关键词的记忆，新会话提问不应报错且正常完成
    （召回经 agent_node 瞬态注入，外部不可直接观测，以全链路无副作用为准）。"""
    kw = uuid.uuid4().hex[:8]
    sid1 = client.mk_session("eval mem cross1")
    evs1 = client.chat(f"你好 请记住暗号{kw}", session_id=sid1)
    client.score("memory:cross_store", evs1, expect="end_turn")
    sid2 = client.mk_session("eval mem cross2")
    evs2 = client.chat(f"暗号{kw} 你好", session_id=sid2)
    fin = client.score("memory:cross_run", evs2, expect="end_turn")
    assert fin.get("stop_reason") == "end_turn", fin
    assert not client.events(evs2, "error")


def test_memory_gate_blocked_not_stored(client):
    """门控未通过（gate_blocked）的 run 不沉淀记忆，避免污染。"""
    tag = uuid.uuid4().hex[:8]
    sid = client.mk_session("eval mem nogate")
    evs = client.chat(f"请保持静默 污染标记{tag}", session_id=sid)
    fin = client.score("memory:no_store_on_block", evs, expect="gate_blocked")
    assert fin.get("stop_reason") == "gate_blocked", fin
    items = client.get("/memories", session_id=sid)["items"]
    assert not any(tag in it["text"] for it in items), items
