"""回归：ContactInfoGuard 不应误伤「微信支付」等站内支付方式，
但仍拦截「加微信/微信号」等引导站外联系。"""

from app.guardrails.output_guards import ContactInfoGuard


def test_wechat_pay_passes():
    g = ContactInfoGuard()
    r = g.check("支持花呗、信用卡、微信支付，满300包邮～")
    assert r.action == "pass"


def test_wechat_solicitation_still_blocked():
    g = ContactInfoGuard()
    for text in ["加微信私聊更便宜", "我的微信号是 abc123", "留个微信方便联系"]:
        r = g.check(text)
        assert r.action == "sanitize", f"应拦截: {text}"
