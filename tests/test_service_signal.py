"""客服侧信号埋点:只在失败轮发、fail-soft、不改主链路返回。

fixture 用真实的 CustomerServiceResponse(而非裸 dict)构造——chat() 里
_publish_service_signal 收到的就是这个类型的对象,不是 dict。上一版拿裸 dict
当输入,掩盖了"用 isinstance(result, dict) 做类型门"这个在真实调用点永远
不成立的判断(chat() 从未传过 dict),这一版直接从真实类型出发,让这类
"契约类型 vs 实际类型"不一致的缺陷能被测试看见。
"""

from unittest.mock import patch

import pytest

from app.schemas.response import CustomerServiceResponse, IntentType


def _response(requires_human: bool, intent: IntentType = IntentType.OTHER) -> CustomerServiceResponse:
    return CustomerServiceResponse(
        intent=intent,
        confidence=0.9,
        reply="ok",
        requires_human=requires_human,
    )


@pytest.fixture()
def agent():
    with patch("app.agent.chat.EcomAgent.__init__", return_value=None):
        from app.agent.chat import EcomAgent
        a = EcomAgent.__new__(EcomAgent)
    a.session_id = "s1"
    a.user_id = "u1"
    return a


def test_success_turn_publishes_nothing(agent):
    result = _response(requires_human=False, intent=IntentType.ORDER_QUERY)
    with patch("app.multi_agent.bus.publish") as pub:
        agent._publish_service_signal(result)
    pub.assert_not_called()


def test_human_escalation_publishes_signal(agent):
    result = _response(requires_human=True, intent=IntentType.RETURN_REQUEST)
    with patch("app.multi_agent.bus.publish") as pub:
        agent._publish_service_signal(result)
    assert pub.called
    call = pub.call_args
    payload = call[0][1] if len(call[0]) > 1 else call[1]["payload"]
    assert payload["kind"] == "service_escalation"
    assert payload["session_id"] == "s1"


def test_publish_failure_is_swallowed(agent):
    """总线挂掉不能让买家那一轮失败。"""
    result = _response(requires_human=True)
    with patch("app.multi_agent.bus.publish", side_effect=RuntimeError("down")):
        agent._publish_service_signal(result)   # 不抛


def test_disabled_switch_publishes_nothing(agent, monkeypatch):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "collab_enabled", False)
    result = _response(requires_human=True)
    with patch("app.multi_agent.bus.publish") as pub:
        agent._publish_service_signal(result)
    pub.assert_not_called()
