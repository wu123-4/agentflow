"""Harness Phase 2 验收脚本：Tracing / 预算仪表化 / 重试与降级 / 模型注册表 / 运行轨迹。

用法: uv run python scripts/phase2_check.py
前提: 后端已运行于 8000 端口，DEV_FAKE_MODEL=true（假模型确定性触发重试/降级），
      OTEL 内存缓冲通道始终开启（无需 Jaeger/Docker 即可断言 span）。
用例对应 docs/07 Phase 2 验收标准。
"""
import contextlib
import json
import os
import sys
import urllib.error
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
    """POST 并解析 SSE 为 [(event, data)]。"""
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


# ---------------------------------------------------------------- T1 Tracing：全链路 span
sid1 = mk_session("H2 tracing")
evs1 = chat_events({"session_id": sid1, "content": "读取 readme 文件并总结",
                    "model": "deepseek-chat"})
fin1 = fin_of(evs1)
run_id = fin1.get("run_id")
trace = req("GET", f"/traces/{run_id}")
items = trace.get("items", [])
names = [s["name"] for s in items]
wf = next((s for s in items if s["name"] == "invoke_workflow"), None)
node_agent = next((s for s in items if s["name"] == "node:agent"), None)
tool_span = next((s for s in items if s["name"].startswith("tool:")), None)
chat_span = next((s for s in items if s["name"] == "chat_model"), None)
check("T1a workflow 根 span 存在", wf is not None, str(names))
check("T1b 节点 span（node:agent）存在", node_agent is not None, str(names))
check("T1c 工具 span 存在且 parent 关联 workflow",
      tool_span is not None and wf is not None and tool_span["parent_id"] == wf["span_id"],
      json.dumps([{k: s[k] for k in ("name", "span_id", "parent_id")} for s in items],
                 ensure_ascii=False)[:400])
check("T1d 模型 span 携带 gen_ai 语义属性与 usage",
      chat_span is not None
      and "gen_ai.request.model" in chat_span["attributes"]
      and chat_span["attributes"].get("gen_ai.usage.output_tokens", 0) > 0,
      json.dumps(chat_span, ensure_ascii=False)[:400] if chat_span else "无 chat_model span")
check("T1e workflow span 记录终态 stop_reason",
      wf is not None and wf["attributes"].get("agentflow.stop_reason") == "end_turn",
      json.dumps(wf, ensure_ascii=False)[:300] if wf else "无 workflow span")

# ---------------------------------------------------------------- T2 预算仪表化：未超限
sid2 = mk_session("H2 budget")
evs2 = chat_events({"session_id": sid2, "content": "读取 readme 文件",
                    "model": "deepseek-chat",
                    "task_contract": {"budgets": {"max_tokens": 100000}}})
fin2 = fin_of(evs2)
bud2 = fin2.get("budget") or {}
used2 = fin2["usage"]["input_tokens"] + fin2["usage"]["output_tokens"]
check("T2a 预算快照随 run_finished 下发（tokens_used 与 usage 一致）",
      bud2.get("tokens_used") == used2 and used2 > 0,
      json.dumps({"bud": bud2, "usage": fin2.get("usage")}, ensure_ascii=False)[:300])
check("T2b 剩余 token 预算可计算",
      bud2.get("tokens_remaining") == 100000 - used2,
      json.dumps(bud2, ensure_ascii=False)[:300])

# ---------------------------------------------------------------- T3 预算超限：预算快照仍下发
sid3 = mk_session("H2 budget exceed")
evs3 = chat_events({"session_id": sid3, "content": "读取 readme 文件",
                    "model": "deepseek-chat",
                    "task_contract": {"budgets": {"max_tokens": 10}}})
fin3 = fin_of(evs3)
bud3 = fin3.get("budget") or {}
check("T3 预算超限干净终止且快照显示已用 > 上限",
      fin3.get("stop_reason") == "budget_exceeded"
      and (bud3.get("tokens_used") or 0) > 10 and bud3.get("max_tokens") == 10,
      json.dumps({"fin": fin3, "bud": bud3}, ensure_ascii=False)[:400])

# ---------------------------------------------------------------- T4 瞬态重试（tenacity）
nonce = uuid.uuid4().hex[:12]
sid4 = mk_session("H2 retry")
evs4 = chat_events({"session_id": sid4, "content": f"重试 {nonce} 你好",
                    "model": "deepseek-chat"})
fin4 = fin_of(evs4)
check("T4a 瞬态故障经 tenacity 重试后成功（未降级）",
      fin4.get("stop_reason") == "end_turn" and fin4.get("fallback_count") == 0,
      json.dumps(fin4, ensure_ascii=False)[:300])
check("T4b 重试成功未下发 fallback 事件",
      not any(e == "fallback" for e, _ in evs4), str([e for e, _ in evs4]))

# ---------------------------------------------------------------- T5 降级链（fallback chain）
sid5 = mk_session("H2 fallback")
evs5 = chat_events({"session_id": sid5, "content": "降级 故障 请直接给出结论",
                    "model": "deepseek-chat"})
fin5 = fin_of(evs5)
fbs5 = [d for e, d in evs5 if e == "fallback"]
check("T5a 主模型不可用触发降级（fallback 事件 from/to 正确）",
      len(fbs5) >= 1 and fbs5[0].get("from") == "deepseek-chat"
      and fbs5[0].get("to") == "qwen-plus",
      json.dumps(fbs5, ensure_ascii=False)[:300])
check("T5b 降级后任务成功结束且 model_used 为备用模型",
      fin5.get("stop_reason") == "end_turn" and fin5.get("model_used") == "qwen-plus"
      and fin5.get("fallback_count", 0) >= 1,
      json.dumps(fin5, ensure_ascii=False)[:300])

# ---------------------------------------------------------------- T6 模型注册表
reg = req("GET", "/models")
ids = [m["id"] for m in reg.get("items", [])]
check("T6a /models 返回注册表且默认模型可见",
      reg.get("default") and reg["default"] in ids
      and all({"provider", "fallback", "context_window"} <= set(m) for m in reg["items"]),
      json.dumps(reg, ensure_ascii=False)[:300])
check("T6b deepseek-chat 配置降级备用 qwen-plus",
      any(m["id"] == "deepseek-chat" and m.get("fallback") == "qwen-plus"
          for m in reg.get("items", [])), str(ids))

# ---------------------------------------------------------------- T7 运行轨迹接口
runs = req("GET", f"/sessions/{sid5}/runs")
items7 = runs.get("items", [])
check("T7 运行轨迹接口聚合 runs（usage/stop_reason/工具计数）",
      len(items7) >= 1
      and all({"usage", "stop_reason", "tool_calls", "started_at"} <= set(r) for r in items7),
      json.dumps(items7, ensure_ascii=False)[:300])

# ---------------------------------------------------------------- T8 会话级模型覆盖
sid8 = mk_session("H2 session model")
req("PATCH", f"/sessions/{sid8}", {"model": "deepseek-chat"})
evs8 = chat_events({"session_id": sid8, "content": "你好"})
fin8 = fin_of(evs8)
trace8 = req("GET", f"/traces/{fin8.get('run_id')}")
wf8 = next((s for s in trace8.get("items", []) if s["name"] == "invoke_workflow"), None)
check("T8 会话级模型覆盖生效（span request.model=会话模型）",
      wf8 is not None and wf8["attributes"].get("gen_ai.request.model") == "deepseek-chat",
      json.dumps(wf8, ensure_ascii=False)[:300] if wf8 else "无 workflow span")

# 清理
for s in sids:
    with contextlib.suppress(Exception):
        req("DELETE", f"/sessions/{s}")

print("\n" + ("Harness Phase 2 ALL PASS ✅" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)