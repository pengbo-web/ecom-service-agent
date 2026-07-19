from app.api.streaming import run_agent_streaming
from app.schemas.response import CustomerServiceResponse, IntentType


class FakeAgent:
    """模拟 EcomAgent：chat 时通过 event_sink 发过程事件，返回结构化结果。"""
    def __init__(self):
        self.event_sink = None

    def chat(self, user_input):
        self.event_sink({"type": "thought", "content": "在查订单"})
        self.event_sink({"type": "tool_call", "name": "get_order", "args": {"id": "A1"}})
        self.event_sink({"type": "tool_result", "content": "已发货"})
        return CustomerServiceResponse(
            intent=IntentType.ORDER_QUERY, confidence=0.9,
            reply="您的订单已发货", requires_human=False, follow_up_question=None,
        )


def test_stream_yields_process_then_reply_then_metadata_then_done():
    events = list(run_agent_streaming(FakeAgent(), "我的订单呢"))
    types = [e["type"] for e in events]
    assert types == ["thought", "tool_call", "tool_result", "reply", "metadata", "done"]
    assert events[3] == {"type": "reply", "content": "您的订单已发货"}
    assert events[4]["intent"] == "order_query"
    assert events[4]["confidence"] == 0.9
    assert events[4]["requires_human"] is False


class BoomAgent:
    def __init__(self):
        self.event_sink = None

    def chat(self, user_input):
        raise RuntimeError("模型炸了")


def test_stream_emits_error_then_done_on_exception():
    events = list(run_agent_streaming(BoomAgent(), "hi"))
    types = [e["type"] for e in events]
    assert types == ["error", "done"]
    assert "模型炸了" in events[0]["message"]
