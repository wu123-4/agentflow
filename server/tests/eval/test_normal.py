"""Phase 4 评估集 · 正常任务（10）：问候 / 读文件 / 总结等 → end_turn + 门控通过。"""
from __future__ import annotations

import pytest

CASES = [
    ("你好，请介绍一下你自己", False),
    ("请问今天有什么可以帮你", False),
    ("问候一下用户", False),
    ("读取 readme 文件", True),
    ("总结 readme.txt 的内容", True),
    ("请读取文件并告诉我里面有什么", True),
    ("帮我看看项目说明文件", True),
    ("读取 readme 然后简要总结", True),
    ("读取 readme 文件内容", True),
    ("简单打个招呼", False),
]


@pytest.mark.parametrize("content,expect_tool", CASES)
def test_normal(client, content, expect_tool):
    sid = client.mk_session("eval normal")
    evs = client.chat(content, session_id=sid)
    fin = client.score(f"normal:{content[:8]}", evs, expect="end_turn")
    assert fin.get("stop_reason") == "end_turn", fin
    assert client.text_of(evs).strip(), "正常任务应有非空答复"
    gate = fin.get("gate_result") or {}
    assert gate.get("passed") is True, gate
    if expect_tool:
        assert any(e == "tool_start" for e, _ in evs), "读文件任务应发起工具调用"
