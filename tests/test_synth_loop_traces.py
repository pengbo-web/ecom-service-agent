"""闭环入口:优先用真实轨迹做失败自改进,无轨迹时回退关键词启发式。"""

from app.scripts.synthesize_skills import run_improvements_from_traces
from tests.test_skill_synth import FakeClient

LIVE_MD = """---
name: process-return
description: 退货处理流程,关键词:退货 退款。
---
现行正文。
"""

IMPROVED_MD = """---
name: process-return
description: 退货处理流程(已按失败案例改进)。
---
第一步：调用 `query_order` 核对订单。
第二步：调用 `apply_refund` 提交退款。
"""


def _definitions(tmp_path):
    d = tmp_path / "definitions"
    (d / "process-return").mkdir(parents=True)
    (d / "process-return" / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")
    return str(d)


def _trace(session_id, skill, outcome):
    return {"session_id": session_id, "skill_name": skill, "outcome": outcome, "tool_calls": []}


def _archive(session_id, text):
    return {"session_id": session_id, "user_id": "u1", "summary": "",
             "messages": [{"role": "user", "content": text},
                          {"role": "assistant", "content": "抱歉,抱歉"}]}


def test_improves_skill_from_failed_traces(tmp_path):
    definitions = _definitions(tmp_path)
    out_dir = tmp_path / "candidates"
    client = FakeClient([IMPROVED_MD])

    paths = run_improvements_from_traces(
        client, "test-model",
        traces=[_trace("s1", "process-return", "handoff")],
        archives=[_archive("s1", "退款一直没人处理")],
        skills_dir=definitions, out_dir=str(out_dir),
    )

    assert len(paths) == 1
    assert paths[0].read_text(encoding="utf-8") == IMPROVED_MD
    assert len(client.calls) == 1


def test_no_failed_traces_no_llm_call(tmp_path):
    definitions = _definitions(tmp_path)
    client = FakeClient([])

    paths = run_improvements_from_traces(
        client, "test-model",
        traces=[_trace("s1", "process-return", "success")],
        archives=[_archive("s1", "怎么退货")],
        skills_dir=definitions, out_dir=str(tmp_path / "c"),
    )

    assert paths == []
    assert client.calls == []


def test_trace_for_unknown_skill_is_ignored(tmp_path):
    """轨迹指向正式库里已不存在的 skill(已删)→ 跳过,不调 LLM。"""
    definitions = _definitions(tmp_path)
    client = FakeClient([])

    paths = run_improvements_from_traces(
        client, "test-model",
        traces=[_trace("s1", "deleted-skill", "handoff")],
        archives=[_archive("s1", "随便问问")],
        skills_dir=definitions, out_dir=str(tmp_path / "c"),
    )

    assert paths == []
    assert client.calls == []
