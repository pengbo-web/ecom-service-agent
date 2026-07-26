from app.api.streaming import run_agent_streaming
from app.agent.consent import is_allowed
from app.schemas.response import CustomerServiceResponse, IntentType


class ConsentProbeAgent:
    """chat() 里探测 consent 门,把结果写进 reply,验证 consent_scope 是否在 chat 执行期间生效。"""
    def __init__(self):
        self.event_sink = None
        self.client = None

    def chat(self, user_input):
        allowed = is_allowed("refund")
        return CustomerServiceResponse(
            intent=IntentType.AFTER_SALE, confidence=0.9,
            reply="allowed" if allowed else "denied",
            requires_human=False, follow_up_question=None,
        )


def _reply(events):
    return [e for e in events if e["type"] == "reply"][0]["content"]


def test_confirm_false_gate_closed():
    events = list(run_agent_streaming(ConsentProbeAgent(), "退款", confirm=False))
    assert _reply(events) == "denied"


def test_confirm_true_without_pending_still_denied():
    # 新契约:无挂起动作时,confirm=True 也不在 normal flow 授权风险动作——
    # 风险动作只走确定性重放(有绑定 pending 时才经 _replay_flow 执行)。
    events = list(run_agent_streaming(ConsentProbeAgent(), "退款", confirm=True))
    assert _reply(events) == "denied"


def test_consent_does_not_leak_after_turn():
    list(run_agent_streaming(ConsentProbeAgent(), "退款", confirm=True))
    # 上一轮授权用后即复位,主线程不受影响
    assert is_allowed("refund") is False


class RefundAgent:
    """授权时回复'已退款',未授权回复'请确认'——用于验证 is_confirmation 授权。"""
    def __init__(self):
        self.event_sink = None
        self.client = None

    def chat(self, user_input):
        reply = "已退款" if is_allowed("refund") else "请确认是否退款?"
        return CustomerServiceResponse(
            intent=IntentType.AFTER_SALE, confidence=0.9, reply=reply,
            requires_human=False, follow_up_question=None,
        )


def test_first_request_not_confirmation_denied():
    # 首次"我要退款"不是确认语 → 未授权
    e = list(run_agent_streaming(RefundAgent(), "我要退款 ORD-1", session_id="s"))
    assert _reply(e) == "请确认是否退款?"


def test_natural_language_confirm_without_pending_denied():
    # 新契约:无挂起动作时自然语言确认不授权,模型只能请用户确认(随后走重放)。
    e = list(run_agent_streaming(RefundAgent(), "确认,退款吧", session_id="s"))
    assert _reply(e) == "请确认是否退款?"
