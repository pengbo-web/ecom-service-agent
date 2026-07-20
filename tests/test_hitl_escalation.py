from app.hitl.escalation import should_escalate, SENSITIVE_INTENTS
from app.hitl.handoff import build_handoff_bundle


def test_escalate_on_requires_human():
    r = should_escalate("order_query", 0.95, True, 0.6, SENSITIVE_INTENTS)
    assert r and any("转人工" in x for x in r)


def test_escalate_on_low_confidence():
    r = should_escalate("order_query", 0.4, False, 0.6, SENSITIVE_INTENTS)
    assert r and any("置信度" in x for x in r)


def test_escalate_on_sensitive_intent():
    r = should_escalate("complaint", 0.95, False, 0.6, {"complaint"})
    assert r and any("敏感意图" in x for x in r)


def test_no_escalation_for_normal():
    assert should_escalate("order_query", 0.95, False, 0.6, SENSITIVE_INTENTS) == []


def test_escalate_on_keyword_even_if_intent_wrong():
    # 模型误判为 product_consult、高置信、不转人工，但用户明说"投诉"
    r = should_escalate("product_consult", 0.95, False, 0.6, SENSITIVE_INTENTS,
                        user_input="你们东西太差，我要投诉！")
    assert r and any("投诉" in x for x in r)


def test_bundle_shape():
    b = build_handoff_bundle("s1", "退款啊", "正在处理", "complaint", 0.5, ["敏感意图(complaint)"])
    assert b["session_id"] == "s1"
    assert b["intent"] == "complaint"
    assert b["reasons"] == ["敏感意图(complaint)"]
    assert "user_input" in b and "reply" in b
