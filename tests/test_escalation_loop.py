"""转人工闭环判定:前置短路/重复无解/QU意图直连/负面情绪。"""

from app.hitl.escalation import (match_human_fast, repeated_unresolved,
                                 should_escalate)
from app.hitl.manager import HitlManager


# ---- 前置短路:只吃纯转人工短句 ----

def test_match_human_fast_pure_requests():
    for t in ["转人工", "人工客服", "找人工", "真人客服", "我要人工", "转人工!"]:
        assert match_human_fast(t) is True, t


def test_match_human_fast_never_eats_normal_questions():
    for t in ["人工审核要多久", "人工客服几点上班", "转人工之前先帮我查下订单",
              "退货要人工审核吗", ""]:
        assert match_human_fast(t) is False, t


# ---- 同一问题重复 N 次 ----

def test_repeated_three_times_triggers():
    prior = ["退款怎么还没到账", "退款怎么还没到啊", "我问商品推荐"]
    assert repeated_unresolved("退款怎么还没到账?", prior, times=3) is True


def test_two_times_not_enough():
    prior = ["退款怎么还没到账"]
    assert repeated_unresolved("退款怎么还没到账?", prior, times=3) is False


def test_short_ack_never_counts_as_repeat():
    prior = ["好的", "好的"]
    assert repeated_unresolved("好的", prior, times=3) is False


def test_different_questions_not_repeat():
    prior = ["退货运费谁承担", "价保期限多久"]
    assert repeated_unresolved("发票抬头怎么改", prior, times=3) is False


# ---- should_escalate 新增原因 ----

def _base(**kw):
    args = dict(intent="other", confidence=0.9, requires_human=False,
                threshold=0.6, sensitive_intents={"complaint"}, user_input="")
    args.update(kw)
    return should_escalate(**args)


def test_qu_intent_human_request_direct():
    reasons = _base(qu_intent="转人工")
    assert any("明确要求转人工" in r for r in reasons)


def test_qu_intent_complaint_direct():
    reasons = _base(qu_intent="投诉")
    assert any("投诉倾向" in r for r in reasons)


def test_repeat_reason_included():
    reasons = _base(user_input="退款怎么还没到账?",
                    prior_user_msgs=["退款怎么还没到账", "退款怎么还没到啊"])
    assert any("重复" in r for r in reasons)


def test_anger_keywords_escalate():
    reasons = _base(user_input="你们就是骗子,垃圾平台")
    assert any("负面情绪" in r for r in reasons)


def test_no_new_reasons_on_calm_normal_turn():
    assert _base(user_input="退货运费谁承担",
                 prior_user_msgs=["之前问的是价保"]) == []


def test_manager_passthrough():
    m = HitlManager(confidence_threshold=0.6)
    reasons = m.evaluate("other", 0.9, False, user_input="随便问问",
                         qu_intent="转人工", prior_user_msgs=[])
    assert any("明确要求转人工" in r for r in reasons)
