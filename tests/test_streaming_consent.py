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


def test_confirm_true_gate_open():
    events = list(run_agent_streaming(ConsentProbeAgent(), "退款", confirm=True))
    assert _reply(events) == "allowed"


def test_consent_does_not_leak_after_turn():
    list(run_agent_streaming(ConsentProbeAgent(), "退款", confirm=True))
    # 上一轮授权用后即复位,主线程不受影响
    assert is_allowed("refund") is False
