"""M2 验收脚本：MCP 管理 API + 连接池热插拔 + 故障剔除。
用法: uv run python scripts/m2_check.py   （需后端已在 8000 端口运行）
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("AGENTFLOW_BASE", "http://127.0.0.1:8000/api/v1")


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read())


def tools_now() -> list[str]:
    return req("GET", "/health")["tools"]


fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# 1. Server 列表：内置 3 个均在，状态 connected
lst = req("GET", "/mcp/servers")["items"]
names = {s["name"] for s in lst}
check("内置 3 Server 注册", {"filesystem", "sqlite-query", "demo-notify"} <= names, str(names))
check("内置 Server 全部 connected",
      all(s["status"] == "connected" for s in lst if s["name"] in names),
      json.dumps({s["name"]: s["status"] for s in lst}))
sq = next(s for s in lst if s["name"] == "sqlite-query")
check("sqlite-query 工具缓存", sq["tools_count"] == 2, str(sq["tools"]))
dn = next(s for s in lst if s["name"] == "demo-notify")
check("demo-notify send_message 风险=high",
      any(t["name"] == "send_message" and t["risk"] == "high" for t in dn["tools"]),
      str(dn["tools"]))

# 2. 工具预览端点
tools = req("GET", f"/mcp/servers/{sq['id']}/tools")
check("GET /servers/{id}/tools", tools["status"] == "connected" and
      {t["name"] for t in tools["tools"]} == {"list_tables", "query"}, str(tools))

# 3. 热插拔：注册新 Server（echo），无需重启即获得工具
before = set(tools_now())
srv = req("POST", "/mcp/servers", {
    "name": "echo-test", "transport": "stdio",
    "command": sys.executable,
    "args": [str(__import__("pathlib").Path(__file__).resolve().parents[2] /
               "mcp-servers" / "echo-test" / "server.py")],
    "enabled": True,
})
check("注册 echo-test", srv["status"] == "connected", json.dumps(srv.get("last_error") or srv["status"]))
after = set(tools_now())
check("热插拔：新工具即时可用", "echo-test__echo" in after, f"before={len(before)} after={len(after)}")

# 3b. 用新工具对话（真实模型或假模型均可调用）
sid = req("POST", "/sessions", {"title": "M2 热插拔"})["id"]
body = json.dumps({"session_id": sid,
                   "content": "调用 echo-test 的 echo 工具，参数 text 为 hello-m2，不要解释"}).encode()
r = urllib.request.Request(BASE + "/chat", data=body, method="POST",
                           headers={"Content-Type": "application/json"})
called = []
with urllib.request.urlopen(r, timeout=180) as resp:
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if line.startswith("data: ") and "echo-test__echo" in line:
            called.append(line)
check("对话可调用新 Server 工具", len(called) > 0, str(called[:1]))

# 4. 禁用 -> 工具消失（热重载）
req("PATCH", f"/mcp/servers/{srv['id']}", {"enabled": False})
gone = set(tools_now())
check("禁用后工具剔除", "echo-test__echo" not in gone)
lst2 = req("GET", "/mcp/servers")["items"]
check("禁用后状态 disconnected",
      next(s for s in lst2 if s["id"] == srv["id"])["status"] == "disconnected")

# 5. 重新启用 -> 工具回来
req("PATCH", f"/mcp/servers/{srv['id']}", {"enabled": True})
back = set(tools_now())
check("重新启用工具恢复", "echo-test__echo" in back)

# 6. 故障剔除：注册一个 command 不存在的 Server -> disconnected 且不影响其它工具
bad = req("POST", "/mcp/servers", {
    "name": "broken-server", "transport": "stdio",
    "command": "definitely-not-a-real-command-xyz", "args": [], "enabled": True})
check("坏 Server 状态 disconnected", bad["status"] == "disconnected", str(bad))
ok_tools = set(tools_now())
check("坏 Server 不影响其它工具", "filesystem__read_text_file" in ok_tools and
      "sqlite-query__query" in ok_tools)

# 7. 冲突校验：重名 409
try:
    req("POST", "/mcp/servers", {"name": "filesystem", "transport": "stdio", "command": "npx"})
    check("重名注册返回 409", False, "未抛错")
except urllib.error.HTTPError as e:
    check("重名注册返回 409", e.code == 409, str(e.code))

# 8. 删除 Server
req("DELETE", f"/mcp/servers/{srv['id']}")
req("DELETE", f"/mcp/servers/{bad['id']}")
final = {s["name"] for s in req("GET", "/mcp/servers")["items"]}
check("删除 Server 生效", "echo-test" not in final and "broken-server" not in final, str(final))
check("删除后工具消失", "echo-test__echo" not in set(tools_now()))

# 9. 清理会话
req("DELETE", f"/sessions/{sid}")

print("\n" + ("M2 ALL PASS ✅" if not fails else f"M2 FAILED: {fails}"))
sys.exit(1 if fails else 0)
