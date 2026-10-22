"""Harness Phase 4 评估体系：pytest 自动起服 + SSE 辅助 + 指标门禁（docs/07 Phase 4）。

- session fixture 在空闲端口拉起 uvicorn 子进程（DEV_FAKE_MODEL/DEV_SQLITE，
  假模型确定性触发，无需 API Key / Docker），轮询 /health 就绪后共享给全部用例。
- EvalClient 封装 REST + SSE 解析（与 scripts/*_check.py 同语义）。
- 全局收集器记录每用例 success / 工具计数 / 恢复 / 拦截 / token，
  pytest_sessionfinish 断言回归门禁：
  Task Success > 85%、工具成功率 > 95%、恢复成功率 > 90%、拦截率 = 100%、
  Token/任务 < 50K。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]

#: 全局指标收集器（每用例 client.record(...) 追加一条）
RESULTS: list[dict] = []


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EvalClient:
    """评估用例的统一入口：REST JSON + SSE 流解析。"""

    def __init__(self, base: str) -> None:
        self.base = base  # http://127.0.0.1:{port}/api/v1

    # ---------------- REST ----------------

    def get(self, path: str, **params):
        r = httpx.get(self.base + path, params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def post_json(self, path: str, body: dict):
        r = httpx.post(self.base + path, json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    def patch(self, path: str, body: dict):
        r = httpx.patch(self.base + path, json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    def delete(self, path: str):
        r = httpx.delete(self.base + path, timeout=60)
        return r.status_code

    def post_status(self, path: str, body: dict) -> int:
        """POST 并返回状态码（不抛异常，用于 422 等负例）。"""
        r = httpx.post(self.base + path, json=body, timeout=60)
        return r.status_code

    # ---------------- SSE ----------------

    def sse(self, path: str, body: dict, timeout: float = 300.0) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        with httpx.Client(timeout=timeout) as c:
            with c.stream("POST", self.base + path, json=body) as r:
                r.raise_for_status()
                ev, cur = None, []
                for line in r.iter_lines():
                    if line.startswith("event: "):
                        ev = line[7:]
                    elif line.startswith("data: "):
                        cur.append(line[6:])
                    elif line == "" and ev:
                        events.append((ev, json.loads("".join(cur))))
                        ev, cur = None, []
        return events

    def chat(self, content: str, *, session_id: str, model: str | None = None,
             contract: dict | None = None, channel: str | None = None
             ) -> list[tuple[str, dict]]:
        body: dict = {"session_id": session_id, "content": content}
        if model:
            body["model"] = model
        if contract is not None:
            body["task_contract"] = contract
        if channel:
            body["channel"] = channel
        return self.sse("/chat", body)

    def resume(self, *, session_id: str, run_id: str, decision: str,
               edited_args: dict | None = None,
               reason: str | None = None) -> list[tuple[str, dict]]:
        return self.sse("/chat/resume", {"session_id": session_id, "run_id": run_id,
                                         "decision": decision,
                                         "edited_args": edited_args, "reason": reason})

    def mk_session(self, title: str) -> str:
        return self.post_json("/sessions", {"title": title})["id"]

    # ---------------- 事件辅助（与 check 脚本同语义） ----------------

    @staticmethod
    def events(evs, name: str) -> list[dict]:
        return [d for e, d in evs if e == name]

    @classmethod
    def fin_of(cls, evs) -> dict:
        fins = cls.events(evs, "run_finished")
        return fins[0] if fins else {}

    @classmethod
    def tool_ends(cls, evs) -> list[dict]:
        return cls.events(evs, "tool_end")

    @classmethod
    def ok_tools(cls, evs) -> list[dict]:
        return [d for d in cls.tool_ends(evs) if d.get("status") == "ok"]

    @classmethod
    def denied_tools(cls, evs) -> list[dict]:
        return [d for d in cls.tool_ends(evs) if d.get("status") == "denied"]

    @classmethod
    def text_of(cls, evs) -> str:
        return "".join(d.get("text", "") for d in cls.events(evs, "token"))

    @classmethod
    def tokens_used(cls, evs) -> int:
        u = cls.fin_of(evs).get("usage") or {}
        return int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))

    # ---------------- 指标记录 ----------------

    def record(self, **kw):
        RESULTS.append({"case": kw.pop("case", "?"), **kw})

    def score(self, case: str, evs, *, expect="end_turn", **extra) -> dict:
        """按预期终态计分并记录指标，返回 run_finished。expect 可为 str 或 tuple。"""
        fin = self.fin_of(evs)
        sr = fin.get("stop_reason")
        executed = [d for d in self.tool_ends(evs)
                    if d.get("status") in ("ok", "error")]
        self.record(
            case=case,
            success=sr in (expect if isinstance(expect, tuple) else (expect,)),
            tool_total=len(executed),
            tool_failed=sum(1 for d in executed if d.get("status") == "error"),
            tokens=self.tokens_used(evs),
            **extra)
        return fin


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="session")
def client():
    """起服 -> 就绪 -> 提供 EvalClient -> teardown 关服。"""
    port = _free_port()
    base = f"http://127.0.0.1:{port}/api/v1"
    env = {**os.environ, "DEV_SQLITE": "true", "DEV_FAKE_MODEL": "true"}
    log_path = SERVER_DIR / "tests" / ".server.log"
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(SERVER_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
    ready = False
    deadline = time.time() + 180  # npx filesystem 冷启动可能较慢
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                if httpx.get(base + "/health", timeout=2).status_code == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        if not ready:
            log.flush()
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
            pytest.fail(f"评估服启动失败（port={port}）\n{tail}")
        yield EvalClient(base)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


# ---------------------------------------------------------------- 指标门禁

def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    if not RESULTS:
        return  # 未收集到任何用例（收集失败/跳过执行），不叠加门禁结论

    n = len(RESULTS)
    success = sum(1 for r in RESULTS if r.get("success"))
    tool_total = sum(r.get("tool_total", 0) for r in RESULTS)
    tool_failed = sum(r.get("tool_failed", 0) for r in RESULTS)
    rec_cases = [r for r in RESULTS if "recovered" in r]
    rec_ok = sum(1 for r in rec_cases if r["recovered"])
    int_cases = [r for r in RESULTS if "intercepted" in r]
    int_ok = sum(1 for r in int_cases if r["intercepted"])
    tokens = sum(r.get("tokens", 0) for r in RESULTS)

    rates = {
        "Task Success": (success / n, 0.85),
        "Tool Success": (((tool_total - tool_failed) / tool_total if tool_total else 1.0), 0.95),
        "Recovery": ((rec_ok / len(rec_cases) if rec_cases else 1.0), 0.90),
        "Interception": ((int_ok / len(int_cases) if int_cases else 1.0), 1.0),
    }
    avg_tokens = tokens / n if n else 0

    lines = [f"指标门禁（{n} 用例，{tool_total} 次工具调用）："]
    gate_fail = False
    for name, (rate, th) in rates.items():
        ok = rate >= th if name != "Interception" else rate == 1.0
        gate_fail |= not ok
        lines.append(f"  {name}: {rate:.2%} (阈值 {'=' if name == 'Interception' else '>'} {th:.0%}) {'PASS' if ok else 'FAIL'}")
    tok_ok = avg_tokens < 50_000
    gate_fail |= not tok_ok
    lines.append(f"  Token/任务: {avg_tokens:.0f} (阈值 < 50000) {'PASS' if tok_ok else 'FAIL'}")
    if gate_fail:
        bad = [r["case"] for r in RESULTS if not r.get("success")]
        lines.append(f"  未达标用例: {bad}")
        session.exitstatus = 1
    print("\n" + "\n".join(lines))
