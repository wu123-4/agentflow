"""Harness Phase 3 验收脚本：分级恢复 / 循环检测 / 上下文治理 / invariants 门控。

用法: uv run python scripts/phase3_check.py
前提: 后端已运行于 8000 端口，DEV_FAKE_MODEL=true、DEV_SQLITE=true（假模型确定性触发）。
用例对应 docs/07 Phase 3 验收标准。
"""
import contextlib
import json
import os
import sys
import urllib.request
import uuid

BASE = os.environ.get("AGENTFLOW_BASE", "http://127.0.0.1:8000/api/v1")

fails = []


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=180) as resp:
        return json.loads(resp.read())


def sse_post(path, body):
    data = json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST",
                               headers={"Content-Type": "application/json"})
    events = []
    ev, cur = None, []
    with urllib.request.urlopen(r, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n").rstrip("\r")
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                cur.append(line[6:])
            elif line == "" and ev:
                events.append((ev, json.loads("".join(cur))))
                ev, cur = None, []
    return events


def chat_events(body):
    return sse_post("/chat", body)


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def fin_of(evs):
    fins = [d for e, d in evs if e == "run_finished"]
    return fins[0] if fins else {}


sids = []


def mk_session(title):
    sid = req("POST", "/sessions", {"title": title})["id"]
    sids.append(sid)
    return sid


# ---------------------------------------------------------------- R1 工具瞬态超时 -> 退避重试成功
nonce = uuid.uuid4().hex[:12]
sid1 = mk_session("P3 tool retry")
evs1 = chat_events({"session_id": sid1, "content": f"工具重试 flaky {nonce} 然后总结"})
fin1 = fin_of(evs1)
tool_ends = [d for e, d in evs1 if e == "tool_end"]
check("R1a 工具瞬态超时经退避重试后成功（end_turn、无 error 事件）",
      fin1.get("stop_reason") == "end_turn"
      and not any(e == "error" for e, _ in evs1),
      json.dumps(fin1, ensure_ascii=False)[:300])
check("R1b 重试后工具真实执行（tool_end 存在且 ok）",
      len(tool_ends) >= 1 and all(d.get("status") == "ok" for d in tool_ends),
      json.dumps(tool_ends, ensure_ascii=False)[:300])
check("R1c 未触发模型降级（重试在工具层解决）",
      fin1.get("fallback_count", 0) == 0, json.dumps(fin1, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- R2 写操作副作用对账
# write_file 为 high 风险 -> 默认契约（max_risk=high）走 HITL，批准后执行。
# 第一次：文件不存在 -> 注入首次超时 -> 对账 absent -> 安全重试写入成功
n2 = uuid.uuid4().hex[:12]
sid2 = mk_session("P3 reconcile")
evs2 = chat_events({"session_id": sid2, "content": f"对账 flaky {n2} 写入演示文件"})
ints2 = [d for e, d in evs2 if e == "interrupt"]
if ints2:
    evs2b = sse_post("/chat/resume", {"session_id": sid2, "run_id": ints2[0]["run_id"],
                                      "interrupt_id": ints2[0]["interrupt_id"],
                                      "decision": "approve"})
else:
    evs2b = evs2
fin2 = fin_of(evs2b)
check("R2a 写操作瞬态失败经对账后安全完成",
      fin2.get("stop_reason") == "end_turn", json.dumps(fin2, ensure_ascii=False)[:300])
# 第二次：**新会话**（避免历史工具结果短路假模型）+ 文件已存在 -> 注入超时 -> 对账 done -> 视为成功不重复执行
n3 = uuid.uuid4().hex[:12]
sid2b = mk_session("P3 reconcile 2")
evs3 = chat_events({"session_id": sid2b, "content": f"对账 flaky {n3} 再次写入"})
ints3 = [d for e, d in evs3 if e == "interrupt"]
if ints3:
    evs3 = sse_post("/chat/resume", {"session_id": sid2b, "run_id": ints3[0]["run_id"],
                                     "interrupt_id": ints3[0]["interrupt_id"],
                                     "decision": "approve"})
fin3 = fin_of(evs3)
# 对账 done 路径不调用真实工具（无 tool_end），结果经 ToolMessage 回喂 -> 最终答复文本可见
txt3 = "".join(d["text"] for e, d in evs3 if e == "token")
recon = "对账结果" in txt3
check("R2b 已发生的写副作用被对账识别（不重复执行）",
      fin3.get("stop_reason") == "end_turn" and recon,
      txt3[:400])

# ---------------------------------------------------------------- R3 循环检测
sid3 = mk_session("P3 loop")
evs3b = chat_events({"session_id": sid3, "content": "循环 请重复执行同一查询"})
fin3b = fin_of(evs3b)
check("R3 连续重复调用被检测并强制终止（stop_reason=loop_detected）",
      fin3b.get("stop_reason") == "loop_detected",
      json.dumps(fin3b, ensure_ascii=False)[:300])

# ---------------------------------------------------------------- R4 上下文治理（消息数阈值）
sid4 = mk_session("P3 context")
compressed = None
for i in range(21):  # 每轮 human+ai=2 条，第 ~20 轮越过 context_max_messages=40
    evs4 = chat_events({"session_id": sid4, "content": f"第{i}轮问候 你好"})
    for e, d in evs4:
        if e == "context_compressed":
            compressed = d
check("R4 长对话触发上下文压缩并下发 context_compressed 事件",
      compressed is not None
      and compressed.get("after_msgs", 10**9) < compressed.get("before_msgs", -1),
      json.dumps(compressed, ensure_ascii=False)[:300] if compressed else "未触发压缩")
fin4 = fin_of(evs4)
check("R4b 压缩后任务仍正常完成（state 未被破坏）",
      fin4.get("stop_reason") == "end_turn", json.dumps(fin4, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- R5 invariants 门控消费
sid5 = mk_session("P3 invariants")
# 契约禁写文件（DENY 回喂）+ no_denied 不变量 -> 自然结束时 gate_blocked
evs5 = chat_events({"session_id": sid5, "content": "写入 保存一个文件",
                    "task_contract": {"max_risk": "low", "invariants": ["no_denied"]}})
fin5 = fin_of(evs5)
gr5 = fin5.get("gate_result") or {}
check("R5a 越权被拦截（denied tool_end 存在）",
      any(d.get("status") == "denied" for e, d in evs5 if e == "tool_end"),
      json.dumps([d for e, d in evs5 if e == "tool_end"], ensure_ascii=False)[:300])
check("R5b invariants 未满足 -> gate_blocked 且 blocked_by 指向不变量",
      fin5.get("stop_reason") == "gate_blocked"
      and str(gr5.get("blocked_by", "")).startswith("invariant:"),
      json.dumps({"fin": fin5, "gate": gr5}, ensure_ascii=False)[:400])

# R5c 满足不变量的正常任务不被误拦
sid6 = mk_session("P3 invariants ok")
evs6 = chat_events({"session_id": sid6, "content": "读取 readme 文件并总结",
                    "task_contract": {"invariants": ["no_denied"]}})
fin6 = fin_of(evs6)
check("R5c 满足 invariants 的正常任务通过门控",
      fin6.get("stop_reason") == "end_turn"
      and (fin6.get("gate_result") or {}).get("passed") is True,
      json.dumps(fin6, ensure_ascii=False)[:300])

# ---------------------------------------------------------------- R6 未知谓词 fail-closed
sid7 = mk_session("P3 invariant unknown")
evs7 = chat_events({"session_id": sid7, "content": "你好",
                    "task_contract": {"invariants": ["nonsense_pred"]}})
fin7 = fin_of(evs7)
check("R6 无法解析的不变量按 fail-closed 拦截",
      fin7.get("stop_reason") == "gate_blocked"
      and "nonsense_pred" in str((fin7.get("gate_result") or {}).get("blocked_by", "")),
      json.dumps(fin7, ensure_ascii=False)[:300])

# ---------------------------------------------------------------- R7 回归：默认契约行为不变
sid8 = mk_session("P3 regression")
evs8 = chat_events({"session_id": sid8, "content": "你好"})
fin8 = fin_of(evs8)
check("R7 默认契约正常结束（无治理/循环/不变量副作用）",
      fin8.get("stop_reason") == "end_turn"
      and not any(e == "context_compressed" for e, _ in evs8),
      json.dumps(fin8, ensure_ascii=False)[:200])

# 清理
for s in sids:
    with contextlib.suppress(Exception):
        req("DELETE", f"/sessions/{s}")

print("\n" + ("Harness Phase 3 ALL PASS ✅" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
