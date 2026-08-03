"""G6 接入:合成/改进的产物必须过工具名校验,编错工具名的候选不写盘。"""

from app.agent.skills.synthesizer import (
    build_tool_hint,
    improve_skill,
    synthesize_one,
    synthesize_skills,
)
from tests.test_skill_synth import FakeClient, REFUND_SAMPLES, REFUND_SKILL_MD

GOOD_TOOLS_MD = """---
name: refund-fast-track
description: 当用户想退款/退货时使用的快速处理流程。
---
第一步：调用 `query_order` 确认订单。
第二步：调用 `apply_refund` 提交退款。
"""

BAD_TOOLS_MD = """---
name: refund-fast-track
description: 当用户想退款/退货时使用的快速处理流程。
---
第一步：调用 `order_list` 拉订单。
"""


def test_build_tool_hint_lists_real_tools():
    hint = build_tool_hint()
    assert "list_user_orders" in hint
    assert "query_coupons" in hint
    assert "只能使用" in hint


def test_synthesize_one_injects_tool_hint_into_prompt():
    client = FakeClient([GOOD_TOOLS_MD])
    synthesize_one(client, "test-model", REFUND_SAMPLES)
    system_msg = client.calls[0]["messages"][0]["content"]
    assert "list_user_orders" in system_msg   # 真实工具清单已注入 system prompt


def test_synthesize_one_rejects_unknown_tool_output():
    client = FakeClient([BAD_TOOLS_MD])
    assert synthesize_one(client, "test-model", REFUND_SAMPLES) is None


def test_synthesize_skills_skips_unknown_tool_candidate(tmp_path):
    client = FakeClient([BAD_TOOLS_MD])
    out = synthesize_skills(client, "test-model", REFUND_SAMPLES, str(tmp_path))
    assert out == []
    assert not (tmp_path / "refund-fast-track").exists()


def test_synthesize_skills_writes_valid_candidate(tmp_path):
    client = FakeClient([GOOD_TOOLS_MD])
    out = synthesize_skills(client, "test-model", REFUND_SAMPLES, str(tmp_path))
    assert len(out) == 1
    assert out[0].read_text(encoding="utf-8") == GOOD_TOOLS_MD


def test_improve_skill_rejects_unknown_tool_output(tmp_path):
    client = FakeClient([BAD_TOOLS_MD])
    skill = {"name": "refund-fast-track", "content": REFUND_SKILL_MD}
    cases = [{"messages": [{"role": "user", "content": "退款没人管"}], "summary": ""}]
    assert improve_skill(client, "test-model", skill, cases, str(tmp_path)) is None


def test_known_tools_can_be_injected_for_isolation(tmp_path):
    """注入自定义工具集时按注入值判定(便于单测/未来多套工具集)。"""
    client = FakeClient([BAD_TOOLS_MD])
    out = synthesize_skills(client, "test-model", REFUND_SAMPLES, str(tmp_path),
                            known_tools={"order_list"})
    assert len(out) == 1


TRAVERSAL_NAME_MD = """---
name: ../process-return
description: 伪装成改进的越权候选。
---
第一步：调用 `query_order`。
"""


def test_traversal_name_candidate_is_rejected(tmp_path):
    """候选名来自 LLM 产物(素材是顾客文本,可被注入):含 ../ 必须拒绝,
    否则会写出候选目录、覆盖线上 skill,绕过门禁与备份。"""
    from tests.test_skill_synth import FakeClient, REFUND_SAMPLES
    from app.agent.skills.synthesizer import synthesize_skills

    client = FakeClient([TRAVERSAL_NAME_MD])
    out = synthesize_skills(client, "test-model", REFUND_SAMPLES, str(tmp_path / "cand"))

    assert out == []
    assert not (tmp_path / "process-return").exists()   # 没有逃出候选目录
