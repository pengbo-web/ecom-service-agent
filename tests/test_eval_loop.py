"""评测闭环:人工回复→评测期望;回流用例→回归集(不覆盖人工维护的用例)。"""

import json

from app.evaluation.trace_to_case import (
    extract_human_reply, keywords_from_reply, trace_to_case,
)
from app.evaluation.case_merge import merge_cases
from app.agent.skills.golden_corpus import HUMAN_AGENT_INTENT


def _archived(*replies):
    msgs = []
    for intent, text in replies:
        msgs.append({"role": "user", "content": "问题"})
        msgs.append({"role": "assistant",
                     "content": json.dumps({"intent": intent, "reply": text},
                                           ensure_ascii=False)})
    return {"messages": msgs}


def test_extract_human_reply_picks_the_human_one():
    a = _archived(("product_consult", "AI 的回答"),
                  (HUMAN_AGENT_INTENT, "您的退款我已经手工加急,今天内到账"))
    assert "手工加急" in extract_human_reply(a)


def test_extract_human_reply_takes_the_last_when_several():
    a = _archived((HUMAN_AGENT_INTENT, "第一次人工回复"),
                  (HUMAN_AGENT_INTENT, "第二次人工回复才是最终答案"))
    assert "第二次" in extract_human_reply(a)


def test_extract_human_reply_empty_without_human():
    assert extract_human_reply(_archived(("product_consult", "只有 AI"))) == ""


def test_extract_tolerates_non_json_assistant():
    a = {"messages": [{"role": "assistant", "content": "纯文本旧格式"}]}
    assert extract_human_reply(a) == ""


def test_keywords_from_reply_returns_content_terms():
    kws = keywords_from_reply("您的退款我已经手工加急处理,今天24点前到账,请留意短信")
    assert kws
    assert all(len(k) >= 2 for k in kws)
    assert not any(k in ("的", "了", "我", "您") for k in kws)


def test_keywords_from_empty_reply_is_empty():
    """抽不出就留空——空期望在评分时被跳过,编造才危险。"""
    assert keywords_from_reply("") == []
    assert keywords_from_reply("好的~") == []


def test_trace_to_case_carries_human_keywords():
    trace = {"trace_id": "abcdef123", "user_input": "退款怎么还没到",
             "intent": "refund", "spans": [{"kind": "hitl"}]}
    case = trace_to_case(trace, human_reply="您的退款我已经手工加急,今天内到账")
    assert case["expected_requires_human"] is True
    assert case["expected_keywords"]


def test_trace_to_case_without_human_reply_has_no_keywords():
    """回归保护:不传人工回复时行为与改造前一致。"""
    trace = {"trace_id": "abc", "user_input": "问题", "intent": "refund", "spans": []}
    case = trace_to_case(trace)
    assert "expected_keywords" not in case or case["expected_keywords"] == []


def test_merge_adds_new_cases():
    existing = [{"id": "hand-1", "description": "人工维护"}]
    incoming = [{"id": "reflow-a", "description": "回流"}]
    merged, added = merge_cases(existing, incoming)
    assert added == 1
    assert {c["id"] for c in merged} == {"hand-1", "reflow-a"}


def test_merge_never_overwrites_existing_id():
    """人工维护的用例是资产,回流不得覆盖它。"""
    existing = [{"id": "hand-1", "description": "人工维护的原始期望"}]
    incoming = [{"id": "hand-1", "description": "回流想覆盖"}]
    merged, added = merge_cases(existing, incoming)
    assert added == 0
    assert merged[0]["description"] == "人工维护的原始期望"


def test_merge_dedupes_within_incoming():
    merged, added = merge_cases([], [{"id": "x"}, {"id": "x"}])
    assert added == 1 and len(merged) == 1
