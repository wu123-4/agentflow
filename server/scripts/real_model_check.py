"""真实模型验证：普通对话 + 工具调用对话，检查 SSE 与 usage。"""
import json
import os
import urllib.request

BASE = os.environ.get("AGENTFLOW_BASE", "http://127.0.0.1:8000/api/v1")


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=180).read())


def chat(session_id, content):
    body = json.dumps({"session_id": session_id, "content": content}).encode()
    r = urllib.request.Request(BASE + "/chat", data=body, method="POST",
                               headers={"Content-Type": "application/json"})
    events = []
    ev, cur = None, []
    with urllib.request.urlopen(r, timeout=180) as resp:
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


sid = req("POST", "/sessions", {"title": "新会话"})["id"]

print("=== 1) 普通对话（真实 qwen-plus）===")
evs = chat(sid, "用一句话介绍你自己")
text = "".join(d["text"] for e, d in evs if e == "token")
print("回复:", text[:160])
fin = next(d for e, d in evs if e == "run_finished")
print("usage:", fin["usage"], "| stop:", fin["stop_reason"])

print("\n=== 2) 工具调用（读 workspace/readme.txt）===")
evs2 = chat(sid, "请调用工具读取 workspace 目录下的 readme.txt 文件，然后总结其内容")
names = [e for e, _ in evs2]
print("事件序列:", names[:3], "...", names[-3:])
ts = [d for e, d in evs2 if e == "tool_start"]
te = [d for e, d in evs2 if e == "tool_end"]
if ts:
    print("工具:", ts[0]["tool"], "| 参数:", json.dumps(ts[0]["args"], ensure_ascii=False)[:100])
if te:
    print("工具状态:", te[0]["status"], "| 耗时:", te[0]["duration_ms"], "ms")
text2 = "".join(d["text"] for e, d in evs2 if e == "token")
print("最终回复:", text2[:200])

ok = bool(text) and fin["usage"]["output_tokens"] > 0 and bool(ts) and bool(te) and te[0]["status"] == "ok"
print("\n" + ("REAL MODEL PASS ✅" if ok else "FAILED ❌"))
