"""回归：ContactInfoGuard 不应误伤「微信支付」等站内支付方式，
但仍拦截「加微信/微信号」等引导站外联系。"""

from app.guardrails.output_guards import ContactInfoGuard


def test_wechat_pay_passes():
    g = ContactInfoGuard()
    r = g.check("支持花呗、信用卡、微信支付，满300包邮～")
    assert r.action == "pass"


def test_wechat_solicitation_still_blocked():
    g = ContactInfoGuard()
    for text in ["加微信私聊更便宜", "我的微信号是 abc123", "留个微信方便联系",
                 "微信联系我", "发微信给你"]:
        r = g.check(text)
        assert r.action == "sanitize", f"应拦截: {text}"


def test_refund_channel_mention_passes():
    """回归(前端实测缺陷):退款说明里列支付渠道是正当话术,不得整条替换。
    旧模式裸词「微信」即命中,把整条正确的取消订单确认回复干掉了。"""
    g = ContactInfoGuard()
    for text in [
        "取消后，款项将原路退回（支付方式：微信/支付宝/银行卡），预计1–3个工作日到账",
        "退款将按您的支付方式（微信、支付宝或银行卡）原路退回",
        "您可以使用微信支付或支付宝付款",
    ]:
        r = g.check(text)
        assert r.action == "pass", f"不应误伤: {text}"
