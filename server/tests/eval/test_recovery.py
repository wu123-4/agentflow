"""Phase 4 评估集 · 恢复（5）：工具瞬态重试 / 写对账 / 循环检测 / 模型重试 / 降级 fallback。"""
from __future__ import annotations

import uuid


def test_recovery_tool_retry(client):
    """工具瞬态超时 → 退避重试成功（不降级、不报错）。"""
    nonce = uuid.uuid4().hex[:12]
    sid = client.mk_session("eval rec tool")
    evs = client.chat(f"工具重试 flaky {nonce} 然后总结", session_id=sid)
    fin = client.score("recovery:tool_retry", evs, expect="end_turn",
                       recovered=True)
    assert fin.get("stop_reason") == "end_turn", fin
    assert not client.events(evs, "error")
    assert all(d.get("status") == "ok" for d in client.tool_ends(evs))
    assert fin.get("fallback_count", 0) == 0


def test_recovery_write_reconcile(client):
    """写操作瞬态失败 → 对账判定 → 安全完成。

    文件不存在 -> absent -> 安全重试执行（tool_end ok）；
    文件已存在 -> done -> 视为成功不重复执行（无 tool_end，答复含"对账结果"）。
    两条路径都是恢复成功。
    """
    sid = client.mk_session("eval rec write")
    n = uuid.uuid4().hex[:8]
    evs = client.chat(f"对账 flaky {n} 写入评估文件", session_id=sid)
    ints = client.events(evs, "interrupt")
    if ints:  # high 风险写默认走 HITL
        it = ints[0]
        evs = client.resume(session_id=sid, run_id=it["run_id"], decision="approve")
    fin = client.score("recovery:write_reconcile", evs, expect="end_turn",
                       recovered=(any(d.get("status") == "ok" for d in client.tool_ends(evs))
                                  or "对账结果" in client.text_of(evs)))
    assert fin.get("stop_reason") == "end_turn", fin


def test_recovery_loop_detect(client):
    """连续重复调用 → 循环检测强制终止（loop_detected）。"""
    sid = client.mk_session("eval rec loop")
    evs = client.chat("循环 请重复执行同一查询", session_id=sid)
    fin = client.score("recovery:loop", evs, expect="loop_detected",
                       recovered=True)
    assert fin.get("stop_reason") == "loop_detected", fin


def test_recovery_model_retry(client):
    """模型瞬时限流 → tenacity 重试成功（未降级）。"""
    nonce = uuid.uuid4().hex[:12]
    sid = client.mk_session("eval rec model")
    evs = client.chat(f"重试 {nonce} 你好", session_id=sid,
                      model="deepseek-chat")
    fin = client.score("recovery:model_retry", evs, expect="end_turn",
                       recovered=True)
    assert fin.get("stop_reason") == "end_turn", fin
    assert fin.get("fallback_count", 0) == 0


def test_recovery_fallback(client):
    """主模型不可用 → 降级链切换备用（fallback 事件 + model_used）。"""
    sid = client.mk_session("eval rec fallback")
    evs = client.chat("降级 故障 请直接给出结论", session_id=sid,
                      model="deepseek-chat")
    fin = client.score("recovery:fallback", evs, expect="end_turn",
                       recovered=True)
    fbs = client.events(evs, "fallback")
    assert fbs and fbs[0]["from"] == "deepseek-chat" and fbs[0]["to"] == "qwen-plus"
    assert fin.get("model_used") == "qwen-plus", fin
    assert fin.get("stop_reason") == "end_turn", fin
