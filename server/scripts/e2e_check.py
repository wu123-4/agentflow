"""端到端 HTTP 验证：sessions -> chat(SSE) -> messages 回放。
用法: uv run python scripts/e2e_check.py
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("AGENTFLOW_BASE", "http://127.0.0.1:8000/api/v1")


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read())


def sse_collect(session_id, content):
    """POST /chat 并解析 SSE，返回事件序列。"""
    body = json.dumps({"session_id": session_id, "content": content}).encode()
    r = urllib.request.Request(BASE + "/chat", data=body, method="POST",
                               headers={"Content-Type": "application/json"})
    events = []
    cur_event, cur_data = None, []
    with urllib.request.urlopen(r, timeout=120) as resp:
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\n").rstrip("\r")
            if line.startswith("event: "):
                cur_event = line[7:]
            elif line.startswith("data: "):
                cur_data.append(line[6:])
            elif line == "":
                if cur_event:
                    events.append((cur_event, json.loads("".join(cur_data))))
                cur_event, cur_data = None, []
    return events


fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# 1. 会话 CRUD
sess = req("POST", "/sessions", {"title": "新会话"})
sid = sess["id"]
check("创建会话", sid.startswith("sess_"))

# 2. 普通对话（假模型问候路径）
ev1 = sse_collect(sid, "你好")
names1 = [e[0] for e in ev1]
check("普通对话事件序列", names1[0] == "run_started" and names1[-1] == "run_finished", str(names1))
check("普通对话有 token 流", "token" in names1)

# 3. 工具调用对话
ev2 = sse_collect(sid, "读取 readme.txt 并总结")
names2 = [e[0] for e in ev2]
check("工具对话含 tool_start", "tool_start" in names2, str(names2))
check("工具对话含 tool_end", "tool_end" in names2)
ts = next(d for e, d in ev2 if e == "tool_start")
te = next(d for e, d in ev2 if e == "tool_end")
check("工具为 filesystem 系列", ts["tool"].startswith("filesystem__"), ts["tool"])
check("工具结果非空", len(te.get("result_preview", "")) > 0, te.get("result_preview", "")[:80])
check("tool_end 状态 ok", te["status"] == "ok")

# 4. 历史回放
msgs = req("GET", f"/sessions/{sid}/messages")["items"]
roles = [m["role"] for m in msgs]
check("历史含 user/assistant 消息", roles.count("user") == 2 and roles.count("assistant") >= 2, str(roles))
asst_tool = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
check("assistant 消息持久化 tool_calls", len(asst_tool) == 1)
check("持久化的工具结果", "AgentFlow" in json.dumps(asst_tool[0]["tool_calls"], ensure_ascii=False))

# 5. 会话列表与标题自动生成
lst = req("GET", "/sessions")["items"]
cur = next(s for s in lst if s["id"] == sid)
check("首条消息自动生成标题", cur["title"] == "你好"[:30] or cur["title"] != "新会话", cur["title"])
check("message_count 正确", cur["message_count"] == len(msgs), f"{cur['message_count']} vs {len(msgs)}")

# 6. 重命名 + 删除
req("PATCH", f"/sessions/{sid}", {"title": "改名测试"})
req("DELETE", f"/sessions/{sid}")
lst2 = req("GET", "/sessions")["items"]
check("删除会话生效", all(s["id"] != sid for s in lst2))

print("\n" + ("ALL PASS ✅" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
