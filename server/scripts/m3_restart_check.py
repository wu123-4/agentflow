"""M3 跨进程恢复验证：后端重启后，对遗留的 waiting_approval run 执行 resume。
用法: uv run python scripts/m3_restart_check.py
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("AGENTFLOW_BASE", "http://127.0.0.1:8000/api/v1")

with open("m3_pending.json", encoding="utf-8") as f:
    st = json.load(f)

data = json.dumps({"session_id": st["session_id"], "run_id": st["run_id"],
                   "interrupt_id": st.get("interrupt_id"), "decision": "approve"}).encode()
r = urllib.request.Request(BASE + "/chat/resume", data=data, method="POST",
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

names = [e for e, _ in events]
print("事件序列:", names)
te = [d for e, d in events if e == "tool_end"]
ok = "tool_start" in names and te and te[0]["status"] == "ok"
print("RESTART RESUME PASS ✅" if ok else "RESTART RESUME FAILED ❌")

# 清理
try:
    urllib.request.urlopen(urllib.request.Request(
        BASE + f"/sessions/{st['session_id']}", method="DELETE"), timeout=30)
except Exception:
    pass
sys.exit(0 if ok else 1)
