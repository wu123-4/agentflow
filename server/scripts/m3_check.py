"""M3 验收脚本：HITL interrupt/resume（approve / edit / reject）+ stop + 错误码。
用法: uv run python scripts/m3_check.py   （需后端已在 8000 端口运行，真实模型）
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("AGENTFLOW_BASE", "http://127.0.0.1:8000/api/v1")
STATE_FILE = "m3_pending.json"  # 供 m3_restart_check.py 使用


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=180) as resp:
        return json.loads(resp.read())


def chat_events(path, body):
    """POST /chat 或 /chat/resume，解析 SSE 为 [(event, data)]。"""
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


fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def trigger_interrupt(sid, extra=""):
    """真实模型有随机性：最多重试 3 轮直到产生 interrupt。"""
    for attempt in range(3):
        prompt = ('调用工具 demo-notify__send_message，参数严格为 '
                  '{"recipient": "ops", "message": "部署完成' + extra + '"}，立即执行，禁止输出任何文字。')
        evs = chat_events("/chat", {"session_id": sid, "content": prompt})
        ints = [d for e, d in evs if e == "interrupt"]
        if ints:
            return evs, ints
    return evs, []


# 1) 高风险工具触发 interrupt
sid = req("POST", "/sessions", {"title": "M3 批准"})["id"]
evs, ints = trigger_interrupt(sid)
check("高风险调用产生 interrupt 事件", len(ints) == 1, str([e for e, _ in evs]))
it = ints[0] if ints else {}
check("interrupt 载荷含 tool/args/risk/run_id",
      it.get("tool") == "demo-notify__send_message" and it.get("risk") == "high"
      and it.get("run_id") and it.get("interrupt_id"), json.dumps(it, ensure_ascii=False)[:200])
fin = [d for e, d in evs if e == "run_finished"]
check("interrupt 后本轮 run_finished(waiting_approval)",
      fin and fin[0]["stop_reason"] == "waiting_approval", str(fin))

# 2) resume approve -> 工具执行
evs2 = chat_events("/chat/resume", {"session_id": sid, "run_id": it["run_id"],
                                    "interrupt_id": it["interrupt_id"], "decision": "approve"})
ts = [d for e, d in evs2 if e == "tool_start"]
te = [d for e, d in evs2 if e == "tool_end"]
check("批准后执行工具", any(d["tool"] == "demo-notify__send_message" for d in ts), str(ts))
check("工具结果 ok", te and te[0]["status"] == "ok", str(te))
check("批准流正常结束", any(e == "run_finished" for e, _ in evs2))
hist = req("GET", f"/sessions/{sid}/messages")["items"]
tc_msgs = [m for m in hist if m.get("tool_calls")]
check("批准后工具调用落库", bool(tc_msgs), str([m["role"] for m in hist]))

# 3) resume edit -> 按新参数执行
sid2 = req("POST", "/sessions", {"title": "M3 改参"})["id"]
evs3, ints3 = trigger_interrupt(sid2, "-原始")
if not ints3:
    check("改参场景触发 interrupt", False, "未触发")
    evs3b, te3, ts3 = [], [], []
else:
    it3 = ints3[0]
    NEW_MSG = "改参验证-唯一标记XYZ"
    evs3b = chat_events("/chat/resume", {"session_id": sid2, "run_id": it3["run_id"],
                                         "interrupt_id": it3["interrupt_id"],
                                         "decision": "edit",
                                         "edited_args": {"recipient": "ops", "message": NEW_MSG}})
    te3 = [d for e, d in evs3b if e == "tool_end"]
    ts3 = [d for e, d in evs3b if e == "tool_start"]
# 改参断言：执行时实际传入的 args 应包含新消息（tool_start.args 或 result 文本）
executed_new = (any(NEW_MSG in json.dumps(d.get("args", {}), ensure_ascii=False) for d in ts3)
                or any(NEW_MSG in d.get("result_preview", "") for d in te3)
                or any("12 字" in d.get("result_preview", "") or "20 字" in d.get("result_preview", "") for d in te3))
check("改参后按新参数执行", bool(te3) and executed_new,
      json.dumps({"ts": ts3, "te": te3}, ensure_ascii=False)[:400])

# 4) resume reject -> 不执行工具，理由回喂
sid3 = req("POST", "/sessions", {"title": "M3 拒绝"})["id"]
evs4, ints4 = trigger_interrupt(sid3)
if not ints4:
    check("拒绝场景触发 interrupt", False, "未触发")
    evs4b, ts4, txt4 = [], [], ""
else:
    it4 = ints4[0]
    evs4b = chat_events("/chat/resume", {"session_id": sid3, "run_id": it4["run_id"],
                                         "interrupt_id": it4["interrupt_id"],
                                         "decision": "reject", "reason": "生产环境禁止外发"})
    ts4 = [d for e, d in evs4b if e == "tool_start"]
    txt4 = "".join(d["text"] for e, d in evs4b if e == "token")
check("拒绝后不执行工具", not ts4, str(ts4))
check("拒绝后仍给出答复", len(txt4) > 0, txt4[:100])
check("拒绝流正常结束", any(e == "run_finished" for e, _ in evs4b) if evs4b else False)

# 5) 错误码：对非中断 run resume -> run_not_interrupted
try:
    r = urllib.request.Request(BASE + "/chat/resume",
                               data=json.dumps({"session_id": sid, "run_id": it["run_id"],
                                                "decision": "approve"}).encode(),
                               method="POST", headers={"Content-Type": "application/json"})
    body = urllib.request.urlopen(r, timeout=30).read().decode()
    check("重复 resume 返回错误事件", "run_not_interrupted" in body, body[:200])
except urllib.error.HTTPError as e:
    check("重复 resume 返回错误事件", True)

# 6) stop 端点（对已完成 run -> stopped=false）
res = req("POST", f"/chat/{it['run_id']}/stop")
check("stop 端点可用", res.get("ok") is True and res.get("stopped") is False, str(res))

# 7) 跨进程重启存活：留一个中断态 run 不落 resume（供 m3_restart_check.py）
sid4 = req("POST", "/sessions", {"title": "M3 重启恢复"})["id"]
evs7, ints7 = trigger_interrupt(sid4)
check("制造待恢复中断", len(ints7) == 1)
with open(STATE_FILE, "w", encoding="utf-8") as f:
    json.dump({"session_id": sid4, **{k: ints7[0][k] for k in ("run_id", "interrupt_id")}}, f)
print(f"待恢复中断已写入 {STATE_FILE}（重启后端后运行 m3_restart_check.py 验证）")

# 清理前三个会话
for s in (sid, sid2, sid3):
    req("DELETE", f"/sessions/{s}")

print("\n" + ("M3 ALL PASS ✅" if not fails else f"M3 FAILED: {fails}"))
sys.exit(1 if fails else 0)
