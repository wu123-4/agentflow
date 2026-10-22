"""Phase 4 评估集 · 边界（5）：空输出门控 / 单工具 / HITL 改参 / 会话级模型覆盖 / 非法契约 422。"""
from __future__ import annotations

import uuid


def test_boundary_empty_output_gate(client):
    """静默 → 空输出被完成门控拦截（gate_blocked）。"""
    sid = client.mk_session("eval boundary silent")
    evs = client.chat("请保持静默不要输出任何内容", session_id=sid)
    fin = client.score("boundary:empty_output", evs, expect="gate_blocked")
    assert fin.get("stop_reason") == "gate_blocked"
    gr = fin.get("gate_result") or {}
    assert gr.get("blocked_by") == "output_non_empty", gr


def test_boundary_gates_disabled(client):
    """gates_enabled=False 时空输出不再拦截（回归兼容开关）。"""
    sid = client.mk_session("eval boundary gates off")
    evs = client.chat("请保持静默", session_id=sid,
                      contract={"gates_enabled": False})
    fin = client.score("boundary:gates_off", evs, expect="end_turn")
    assert fin.get("stop_reason") == "end_turn"
    assert "gate_result" not in fin


def test_boundary_edit_resume(client):
    """高风险写 → HITL → edit 改参执行。"""
    sid = client.mk_session("eval boundary edit")
    evs = client.chat("写入 保存一个演示文件 " + uuid.uuid4().hex[:8], session_id=sid)
    ints = client.events(evs, "interrupt")
    assert ints, "high 风险写应触发 HITL"
    it = ints[0]
    new_path = it["args"]["path"].replace("harness_demo.txt", "harness_edit_ok.txt")
    evs2 = client.resume(session_id=sid, run_id=it["run_id"], decision="edit",
                         edited_args={**it["args"], "path": new_path})
    fin = client.score("boundary:edit_resume", evs2, expect="end_turn")
    assert fin.get("stop_reason") == "end_turn", fin
    assert any(d.get("status") == "ok" for d in client.tool_ends(evs2)), \
        "edit 改参后工具应真实执行"


def test_boundary_session_model_override(client):
    """会话级模型配置生效（PATCH model 后 run 使用会话模型）。"""
    sid = client.mk_session("eval boundary model")
    client.patch(f"/sessions/{sid}", {"model": "qwen-plus"})
    evs = client.chat("你好", session_id=sid)
    fin = client.score("boundary:session_model", evs, expect="end_turn")
    assert fin.get("stop_reason") == "end_turn"
    # 会话模型无降级链；trace 的工作流 span 应记录该模型
    trace = client.get(f"/traces/{fin['run_id']}")
    wf = next((s for s in trace["items"] if s["name"] == "invoke_workflow"), None)
    assert wf and wf["attributes"].get("gen_ai.request.model") == "qwen-plus", trace


def test_boundary_invalid_contract_422(client):
    """非法契约（max_risk 非枚举值）→ API 层 422。"""
    sid = client.mk_session("eval boundary 422")
    code = client.post_status("/chat", {"session_id": sid, "content": "你好",
                                        "task_contract": {"max_risk": "extreme"}})
    client.record(case="boundary:invalid_contract", success=(code == 422))
    assert code == 422
