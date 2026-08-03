"""G5 金牌客服语料:人工接管(intent=human_agent)的会话才是"人救过场"的优质样本。"""

import json

from app.agent.skills.golden_corpus import (
    GOLDEN_SYSTEM_PROMPT,
    extract_golden_samples,
    is_human_handled,
    synthesize_from_golden,
)
from tests.test_skill_synth import FakeClient

GOLDEN_MD = """---
name: refund-human-playbook
description: 用户退款受阻升级处理时的人工经验流程。
---
第一步：调用 `query_order` 核对订单。
第二步：调用 `apply_refund` 提交并同步进度。
"""


def _human_msg(text):
    return {"role": "assistant", "content": json.dumps(
        {"intent": "human_agent", "confidence": 1.0, "reply": text,
         "requires_human": False, "follow_up_question": None}, ensure_ascii=False)}


def _ai_msg(text):
    return {"role": "assistant", "content": json.dumps(
        {"intent": "return_request", "confidence": 0.9, "reply": text,
         "requires_human": False, "follow_up_question": None}, ensure_ascii=False)}


def _archive(session_id, msgs, summary=""):
    return {"session_id": session_id, "user_id": "u1", "summary": summary, "messages": msgs}


def test_is_human_handled_detects_human_agent_intent():
    a = _archive("s1", [{"role": "user", "content": "要退货"}, _human_msg("我已帮您登记")])
    assert is_human_handled(a) is True


def test_pure_ai_session_not_human_handled():
    a = _archive("s2", [{"role": "user", "content": "要退货"}, _ai_msg("请提供订单号")])
    assert is_human_handled(a) is False


def test_plain_text_assistant_content_not_human_handled():
    """assistant 内容不是 JSON(旧格式/纯文本)时不误判成人工。"""
    a = _archive("s3", [{"role": "user", "content": "hi"},
                        {"role": "assistant", "content": "你好"}])
    assert is_human_handled(a) is False


def test_extract_golden_samples_filters_to_human_sessions():
    archives = [
        _archive("s1", [{"role": "user", "content": "退货被拒"}, _human_msg("我特批给您")]),
        _archive("s2", [{"role": "user", "content": "查订单"}, _ai_msg("已查到")]),
    ]
    samples = extract_golden_samples(archives)
    assert [s["session_id"] for s in samples] == ["s1"]


def test_golden_prompt_mentions_human_expert():
    assert "人工" in GOLDEN_SYSTEM_PROMPT


def test_synthesize_from_golden_writes_candidate(tmp_path):
    archives = [
        _archive("s1", [{"role": "user", "content": "退款一直不到账"}, _human_msg("我帮您加急")]),
        _archive("s2", [{"role": "user", "content": "退货运费谁承担"}, _human_msg("这单我们承担")]),
    ]
    client = FakeClient([GOLDEN_MD])
    out = synthesize_from_golden(client, "test-model", archives, str(tmp_path))

    assert len(out) == 1
    assert out[0].read_text(encoding="utf-8") == GOLDEN_MD
    system_msg = client.calls[0]["messages"][0]["content"]
    assert "人工" in system_msg


def test_synthesize_from_golden_no_human_sessions_no_llm_call(tmp_path):
    archives = [_archive("s2", [{"role": "user", "content": "查订单"}, _ai_msg("已查到")])]
    client = FakeClient([])
    out = synthesize_from_golden(client, "test-model", archives, str(tmp_path))

    assert out == []
    assert client.calls == []


def test_single_human_session_still_produces_candidate(tmp_path):
    """金牌语料稀少:单条人工接管会话也应产出候选(不套用"同类≥2"门槛)。"""
    archives = [_archive("s1", [{"role": "user", "content": "退款一直不到账"},
                                _human_msg("我帮您加急")])]
    client = FakeClient([GOLDEN_MD])
    out = synthesize_from_golden(client, "test-model", archives, str(tmp_path))

    assert len(out) == 1
    assert len(client.calls) == 1


def test_golden_prompt_forbids_autonomous_concessions():
    """授权红线:让利/补偿类酌情决定不得被归纳成 AI 可自主执行的步骤。"""
    assert "授权红线" in GOLDEN_SYSTEM_PROMPT
    assert "不得" in GOLDEN_SYSTEM_PROMPT
    assert "转人工" in GOLDEN_SYSTEM_PROMPT
    assert "主动让利" not in GOLDEN_SYSTEM_PROMPT
