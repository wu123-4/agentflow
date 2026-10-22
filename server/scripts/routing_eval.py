"""通道路由评测（AgentHarness-通道路由改造方案 §3.6 / §12）。

20 条任务 = 10 单查询（fast）+ 5 多步（calc）+ 5 报告（deep）。
两轮执行：
  A) 三档：按 router 判定自动分档
  B) 基线：全部强制 deep（单通道基线）
对比两轮的 P50 耗时 / 平均 token / 成功率 / 编造出处次数，产出 §3.6 表；
误升级率与分档准确率来自 A 轮，用于回填 §12 调参记录。

用法（需后端已启动，且使用真实模型）：
    python scripts/routing_eval.py
    AGENTFLOW_BASE=http://127.0.0.1:8000/api/v1 python scripts/routing_eval.py
    python scripts/routing_eval.py --only single   # 只跑单查询组（冒烟/省额度）

说明：本脚本会真实调用模型，按 20 条任务 × 2 轮计费，请确认额度后再全量运行。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
import urllib.request

BASE = os.environ.get("AGENTFLOW_BASE", "http://127.0.0.1:8000/api/v1")

#: 报告中引用的 query_id 必须来自真实工具输出（防编造）
_QID_RE = re.compile(r"q_[0-9a-f]{8}")

#: 任务集：group ∈ single|multi|report；expect 为预期通道（§3.3）
TASKS: list[dict] = [
    # ---- 10 单查询 → fast（一次确定性取数即可作答） ----
    {"id": "s1", "group": "single", "expect": "fast", "q": "华东区一共卖了多少销售额？"},
    {"id": "s2", "group": "single", "expect": "fast", "q": "一共有多少个客户？"},
    {"id": "s3", "group": "single", "expect": "fast", "q": "数码品类一共有多少个商品？"},
    {"id": "s4", "group": "single", "expect": "fast", "q": "Q3 一共产生了多少张订单？"},
    {"id": "s5", "group": "single", "expect": "fast", "q": "有多少张订单是已退款状态？"},
    {"id": "s6", "group": "single", "expect": "fast", "q": "华南区有多少个客户？"},
    {"id": "s7", "group": "single", "expect": "fast", "q": "数据里一共有几个地区？"},
    {"id": "s8", "group": "single", "expect": "fast", "q": "服饰品类一共卖出多少件商品？"},
    {"id": "s9", "group": "single", "expect": "fast", "q": "订单明细一共有多少条记录？"},
    {"id": "s10", "group": "single", "expect": "fast", "q": "哪个地区的销售额最高，是多少？"},
    # ---- 5 多步分析 → calc（需多次取数/试错/对比） ----
    {"id": "m1", "group": "multi", "expect": "calc",
     "q": "先看有哪些表，再统计各地区的销售额，并说明哪个地区最高"},
    {"id": "m2", "group": "multi", "expect": "calc",
     "q": "哪个地区的退款率最高？请给出计算过程和结论"},
    {"id": "m3", "group": "multi", "expect": "calc",
     "q": "对比各品类的销售额与订单数，指出表现最好的品类"},
    {"id": "m4", "group": "multi", "expect": "calc",
     "q": "数码品类 7 月到 9 月的销售额趋势如何，是上升还是下降？"},
    {"id": "m5", "group": "multi", "expect": "calc",
     "q": "客单价最高的城市是哪个？请分步骤取数后给出结论"},
    # ---- 5 报告交付 → deep（交付物，须引用真实 query_id） ----
    {"id": "d1", "group": "report", "expect": "deep", "q": "生成一份 2026 Q3 经营分析报告"},
    {"id": "d2", "group": "report", "expect": "deep", "q": "写一份各地区销售对比报告"},
    {"id": "d3", "group": "report", "expect": "deep", "q": "输出一份品类分析报告，包含图表"},
    {"id": "d4", "group": "report", "expect": "deep", "q": "整理一份 Q3 客户与订单分析报告"},
    {"id": "d5", "group": "report", "expect": "deep", "q": "撰写一份 Q3 销售总结报告"},
]

REPORT_CONTRACT = {"invariants": ["report_cites_queries"]}


def _req(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=180) as resp:
        return json.loads(resp.read())


def _sse(path: str, body: dict) -> list[tuple[str, dict]]:
    data = json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST",
                               headers={"Content-Type": "application/json"})
    events: list[tuple[str, dict]] = []
    ev, cur = None, []
    with urllib.request.urlopen(r, timeout=600) as resp:
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


def run_one(task: dict, *, channel: str | None) -> dict:
    """跑一条任务，返回指标行。channel=None 表示三档自动判定。"""
    sid = _req("POST", "/sessions", {"title": f"eval {task['id']}"})["id"]
    body: dict = {"session_id": sid, "content": task["q"]}
    if task["group"] == "report":
        body["task_contract"] = REPORT_CONTRACT  # 两轮一致：报告任务都校验引用出处
    if channel:
        body["channel"] = channel
    t0 = time.monotonic()
    evs = _sse("/chat", body)
    elapsed = time.monotonic() - t0

    def of(name: str) -> list[dict]:
        return [d for e, d in evs if e == name]

    fin = (of("run_finished") or [{}])[0]
    usage = fin.get("usage") or {}
    tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    answer = "".join(d.get("text", "") for d in of("token"))
    known = {q for d in of("tool_end") for q in _QID_RE.findall(str(d.get("result_preview", "")))}
    cited = set(_QID_RE.findall(answer))
    fabricated = len(cited - known)
    tools_ok = [d for d in of("tool_end") if d.get("status") == "ok"]
    return {
        "id": task["id"], "group": task["group"], "expect": task["expect"],
        "channel": (of("channel_selected") or [{}])[0].get("channel"),
        "final_channel": fin.get("channel"),
        "stop_reason": fin.get("stop_reason"),
        "blocked_by": (fin.get("gate_result") or {}).get("blocked_by"),
        "escalations": fin.get("escalations") or [],
        "elapsed_s": round(elapsed, 2), "tokens": tokens,
        "has_answer": bool(answer.strip()),
        "tool_ok": len(tools_ok),
        "fabricated": fabricated,
        "answer": answer[:200],
    }


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    k = max(0, min(len(xs) - 1, round((p / 100) * (len(xs) - 1))))
    return xs[k]


def summarize(rows: list[dict], *, baseline: bool) -> dict:
    ok = [r for r in rows if r["stop_reason"] == "end_turn" and r["has_answer"]
          and r["fabricated"] == 0]
    by_ch = {"fast": [], "calc": [], "deep": []}
    for r in rows:
        by_ch.setdefault(r["final_channel"] or "deep", []).append(r)
    out = {
        "n": len(rows),
        "success": len(ok) / len(rows) if rows else 0.0,
        "p50": _pct([r["elapsed_s"] for r in rows], 50),
        "p95": _pct([r["elapsed_s"] for r in rows], 95),
        "avg_tokens": statistics.mean([r["tokens"] for r in rows]) if rows else 0.0,
        "fabricated": sum(r["fabricated"] for r in rows),
    }
    if not baseline:
        hit = sum(1 for r in rows if r["final_channel"] == r["expect"])
        out["route_acc"] = hit / len(rows) if rows else 0.0
        fast_rows = [r for r in rows if r["expect"] == "fast"]
        out["over_escalation"] = (
            sum(1 for r in fast_rows if r["final_channel"] != "fast") / len(fast_rows)
            if fast_rows else 0.0)
    for ch, rs in by_ch.items():
        if rs:
            out[f"{ch}_n"] = len(rs)
            out[f"{ch}_p50"] = _pct([r["elapsed_s"] for r in rs], 50)
            out[f"{ch}_tokens"] = round(statistics.mean([r["tokens"] for r in rs]))
    return out


def _fmt_table(tiers: dict, base: dict) -> str:
    def f(v, pct=False):
        return f"{v:.1%}" if pct else (f"{v:.1f}" if isinstance(v, float) else str(v))
    lines = [
        "| 指标 | 全部 deep（基线） | 三档 | 变化 |",
        "|---|---|---|---|",
        (f"| P50 耗时(s) | {base['p50']:.1f} | {tiers['p50']:.1f} | "
         f"{(tiers['p50'] - base['p50']):+.1f} |"),
        (f"| P95 耗时(s) | {base['p95']:.1f} | {tiers['p95']:.1f} | "
         f"{(tiers['p95'] - base['p95']):+.1f} |"),
        (f"| 平均 token | {base['avg_tokens']:.0f} | {tiers['avg_tokens']:.0f} | "
         f"{(tiers['avg_tokens'] - base['avg_tokens']):+.0f} |"),
        f"| 分档准确率 | — | {tiers.get('route_acc', 0):.1%} | — |",
        f"| 误升级率 | — | {tiers.get('over_escalation', 0):.1%} | — |",
        f"| 任务成功率 | {f(base['success'], True)} | {f(tiers['success'], True)} | — |",
        f"| 编造出处次数 | {base['fabricated']} | {tiers['fabricated']} | — |",
    ]
    return "\n".join(lines)


def _fmt_per_channel(rows: list[dict]) -> str:
    lines = ["| 通道 | 任务数 | P50 耗时(s) | 平均 token |", "|---|---|---|---|"]
    for ch in ("fast", "calc", "deep"):
        rs = [r for r in rows if (r["final_channel"] or "deep") == ch]
        if not rs:
            continue
        lines.append(f"| {ch} | {len(rs)} | {_pct([r['elapsed_s'] for r in rs], 50):.1f} | "
                     f"{statistics.mean([r['tokens'] for r in rs]):.0f} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["single", "multi", "report"], default=None,
                    help="只跑某一组任务（冒烟/省额度）")
    ap.add_argument("--limit", type=int, default=0,
                    help="只跑前 N 条（0 = 全部），用于真实模型的小步冒烟")
    args = ap.parse_args()
    tasks = [t for t in TASKS if not args.only or t["group"] == args.only]
    if args.limit:
        tasks = tasks[:args.limit]

    print(f"=== 三档运行（{len(tasks)} 条）===", flush=True)
    tiers_rows = []
    for t in tasks:
        row = run_one(t, channel=None)
        tiers_rows.append(row)
        mark = "OK" if row["final_channel"] == row["expect"] else "MISS"
        print(f"[{mark}] {row['id']} {row['expect']}->{row['final_channel']} "
              f"{row['elapsed_s']}s {row['tokens']}tok esc={len(row['escalations'])} "
              f"fabricated={row['fabricated']} stop={row['stop_reason']}"
              f"{'/' + row['blocked_by'] if row['blocked_by'] else ''}", flush=True)
        if row["fabricated"]:
            print(f"      answer={row['answer']!r}", flush=True)

    print(f"\n=== 基线运行：全部 deep（{len(tasks)} 条）===", flush=True)
    base_rows = []
    for t in tasks:
        row = run_one(t, channel="deep")
        base_rows.append(row)
        print(f"[{row['id']}] deep {row['elapsed_s']}s {row['tokens']}tok "
              f"fabricated={row['fabricated']} stop={row['stop_reason']}", flush=True)

    tiers = summarize(tiers_rows, baseline=False)
    base = summarize(base_rows, baseline=True)

    print("\n## §3.6 基线对比表\n")
    print(_fmt_table(tiers, base))
    print("\n## 按通道分组\n")
    print(_fmt_per_channel(tiers_rows))
    print("\n## 明细（三档）\n")
    print("| id | 组 | 预期 | 实际 | 耗时(s) | token | 升级 | 编造 | stop |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in tiers_rows:
        esc = ",".join(f"{e['from']}->{e['to']}:{e['reason']}" for e in r["escalations"]) or "-"
        print(f"| {r['id']} | {r['group']} | {r['expect']} | {r['final_channel']} | "
              f"{r['elapsed_s']} | {r['tokens']} | {esc} | {r['fabricated']} | "
              f"{r['stop_reason']} |")


if __name__ == "__main__":
    main()
