"""Harness Phase 1 验收脚本：Task Contract / AuthorityController / CompletionGate / 预算强制。

用法: uv run python scripts/harness_check.py
前提: 后端已在 8000 端口运行，且 DEV_FAKE_MODEL=true（假模型保证确定性触发）。
用例对应 docs/01 FR-25~28 与 docs/07 Phase 1 验收标准。
"""
import contextlib
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("AGENTFLOW_BASE", "http://127.0.0.1:8000/api/v1")

fails = []


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=180) as resp:
        return json.loads(resp.read())


def chat_events(body):
    """POST /chat 并解析 SSE 为 [(event, data)]。"""
    return sse_post("/chat", body)


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


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def fin_of(evs):
    fins = [d for e, d in evs if e == "run_finished"]
    return fins[0] if fins else {}


# ---------------------------------------------------------------- T1 契约缺省：回归兼容
sid = req("POST", "/sessions", {"title": "H1 默认契约"})["id"]
evs = chat_events({"session_id": sid, "content": "读取 readme 文件并总结"})
fin = fin_of(evs)
ts = [d for e, d in evs if e == "tool_start"]
te = [d for e, d in evs if e == "tool_end"]
check("T1 默认契约工具正常执行（回归兼容）",
      any(d["tool"] == "filesystem__read_text_file" for d in ts)
      and te and te[0]["status"] == "ok"
      and fin.get("stop_reason") == "end_turn"
      and fin.get("gate_result", {}).get("passed") is True,
      json.dumps(fin, ensure_ascii=False)[:300])

# ---------------------------------------------------------------- T2 契约限权：越权 100% 拦截
sid2 = req("POST", "/sessions", {"title": "H1 只读契约"})["id"]
evs2 = chat_events({"session_id": sid2, "content": "把总结写入文件保存",
                    "task_contract": {"max_risk": "low", "outcome": "只读调研"}})
fin2 = fin_of(evs2)
ts2 = [d for e, d in evs2 if e == "tool_start"]
te2 = [d for e, d in evs2 if e == "tool_end"]
denied_te = [d for d in te2 if d.get("status") == "denied"]
txt2 = "".join(d["text"] for e, d in evs2 if e == "token")
check("T2a 越权调用 100% 拦截（write_file 未执行）",
      not any(d["tool"] == "filesystem__write_file" for d in ts2), str(ts2))
check("T2b 拒绝以 denied tool_end 事件下发",
      len(denied_te) == 1 and "max_risk" in denied_te[0]["result_preview"],
      json.dumps(denied_te, ensure_ascii=False)[:300])
check("T2c 拒绝原因回喂模型并引导调整策略",
      "风险等级 high 超出契约允许上限" in txt2, txt2[:200])
check("T2d 拦截后任务仍正常结束", fin2.get("stop_reason") == "end_turn",
      json.dumps(fin2, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- T3 契约内高风险：HITL 链路保留
sid3 = req("POST", "/sessions", {"title": "H1 契约内HITL"})["id"]
evs3 = chat_events({"session_id": sid3, "content": "发送部署完成通知",
                    "task_contract": {"max_risk": "high"}})
ints3 = [d for e, d in evs3 if e == "interrupt"]
check("T3a 契约内 high 风险仍触发 HITL interrupt", len(ints3) == 1,
      str([e for e, _ in evs3]))
if ints3:
    it3 = ints3[0]
    evs3b = sse_post("/chat/resume", {"session_id": sid3, "run_id": it3["run_id"],
                                      "interrupt_id": it3["interrupt_id"],
                                      "decision": "reject",
                                      "reason": "验证契约内 HITL 拒绝"})
    ts3 = [d for e, d in evs3b if e == "tool_start"]
    fin3 = fin_of(evs3b)
    check("T3b 拒绝后工具未执行", not ts3, str(ts3))
    check("T3c 拒绝流正常结束", fin3.get("stop_reason") == "end_turn",
          json.dumps(fin3, ensure_ascii=False)[:200])
else:
    check("T3b 拒绝后工具未执行", False, "未触发 interrupt")
    check("T3c 拒绝流正常结束", False, "未触发 interrupt")

# ---------------------------------------------------------------- T4 工具调用预算
sid4 = req("POST", "/sessions", {"title": "H1 工具预算"})["id"]
evs4 = chat_events({"session_id": sid4, "content": "读取 readme 文件",
                    "task_contract": {"budgets": {"max_tool_calls": 0}}})
fin4 = fin_of(evs4)
ts4 = [d for e, d in evs4 if e == "tool_start"]
te4 = [d for e, d in evs4 if e == "tool_end"]
check("T4 超出工具调用预算：调用前拦截",
      not ts4 and any(d.get("status") == "denied" and "max_tool_calls" in d["result_preview"]
                      for d in te4)
      and fin4.get("stop_reason") == "end_turn",
      json.dumps({"ts": ts4, "fin": fin4}, ensure_ascii=False)[:400])

# ---------------------------------------------------------------- T5 token 预算：干净终止
sid5 = req("POST", "/sessions", {"title": "H1 token预算"})["id"]
evs5 = chat_events({"session_id": sid5, "content": "读取 readme 文件",
                    "task_contract": {"budgets": {"max_tokens": 10}}})
fin5 = fin_of(evs5)
errs5 = [d for e, d in evs5 if e == "error"]
check("T5 token 预算超限干净终止（stop_reason=budget_exceeded）",
      fin5.get("stop_reason") == "budget_exceeded"
      and any(d.get("code") == "budget_exceeded" for d in errs5),
      json.dumps({"fin": fin5, "errs": errs5}, ensure_ascii=False)[:400])

# ---------------------------------------------------------------- T6 完成门控
sid6 = req("POST", "/sessions", {"title": "H1 门控拦截"})["id"]
evs6 = chat_events({"session_id": sid6, "content": "请静默",
                     "task_contract": {"gates_enabled": True}})
fin6 = fin_of(evs6)
gr6 = fin6.get("gate_result") or {}
check("T6a 空输出被完成门控拦截（stop_reason=gate_blocked）",
      fin6.get("stop_reason") == "gate_blocked"
      and gr6.get("passed") is False and gr6.get("blocked_by") == "output_non_empty",
      json.dumps(fin6, ensure_ascii=False)[:300])

sid6b = req("POST", "/sessions", {"title": "H1 门控关闭"})["id"]
evs6b = chat_events({"session_id": sid6b, "content": "请静默",
                     "task_contract": {"gates_enabled": False}})
fin6b = fin_of(evs6b)
check("T6b 关闭门控后同一场景正常结束（end_turn）",
      fin6b.get("stop_reason") == "end_turn" and "gate_result" not in fin6b,
      json.dumps(fin6b, ensure_ascii=False)[:300])

# ---------------------------------------------------------------- T7 非法契约：API 422
code = None
try:
    req("POST", "/chat", {"session_id": sid, "content": "你好",
                          "task_contract": {"max_risk": "extreme"}})
except urllib.error.HTTPError as e:
    code = e.code
check("T7 非法契约被 pydantic 校验拦截（422）", code == 422, str(code))

# ---------------------------------------------------------------- T8 契约持久化与回显
evs8 = chat_events({"session_id": sid, "content": "读取 readme 文件",
                    "task_contract": {"max_risk": "medium",
                                      "budgets": {"max_tool_calls": 5}}})
starts8 = [d for e, d in evs8 if e == "run_started"]
check("T8 契约随 run_started 回显（落库字段同源）",
      starts8 and starts8[0].get("task_contract", {}).get("max_risk") == "medium"
      and starts8[0]["task_contract"]["budgets"]["max_tool_calls"] == 5,
      json.dumps(starts8, ensure_ascii=False)[:300])

# 清理
for s in (sid, sid2, sid3, sid4, sid5, sid6, sid6b):
    with contextlib.suppress(Exception):
        req("DELETE", f"/sessions/{s}")

print("\n" + ("Harness Phase 1 ALL PASS ✅" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
