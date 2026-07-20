import itertools

from app.api.streaming import run_agent_streaming
from app.observability.store import TraceStore
from app.observability.tracer import Tracer
from app.schemas.response import CustomerServiceResponse, IntentType


class _FakeClientPart:
    """让 agent.client.chat.completions.create 可被代理包装。"""
    class _C:
        class _Comp:
            def create(self, **kw): ...
        def __init__(self): self.completions = self._Comp()
    class _Beta:
        class _C2:
            class _Comp2:
                def parse(self, **kw): ...
            def __init__(self): self.completions = self._Comp2()
        def __init__(self): self.chat = self._C2()
    def __init__(self):
        self.chat = self._C()
        self.beta = self._Beta()


class FakeAgent:
    def __init__(self):
        self.event_sink = None
        self.client = _FakeClientPart()

    def chat(self, user_input):
        self.event_sink({"type": "tool_call", "name": "query_order", "args": {}})
        self.event_sink({"type": "tool_result", "content": '{"success": true}'})
        return CustomerServiceResponse(
            intent=IntentType.ORDER_QUERY, confidence=0.9,
            reply="已发货", requires_human=False, follow_up_question=None,
        )


def _tracer(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    clock = itertools.count()
    ids = itertools.count(1)
    return Tracer(store, now=lambda: next(clock), id_factory=lambda: f"id{next(ids)}"), store


def test_streaming_without_tracer_unchanged():
    events = list(run_agent_streaming(FakeAgent(), "查订单"))
    assert [e["type"] for e in events] == \
        ["tool_call", "tool_result", "reply", "metadata", "done"]


def test_streaming_with_tracer_persists_trace(tmp_path):
    tracer, store = _tracer(tmp_path)
    events = list(run_agent_streaming(FakeAgent(), "查订单", tracer=tracer, session_id="sess1"))
    assert [e["type"] for e in events][-1] == "done"

    traces = store.recent_traces()
    assert len(traces) == 1
    assert traces[0]["intent"] == "order_query"
    assert traces[0]["status"] == "ok"

    detail = store.get_trace(traces[0]["trace_id"])
    tool_spans = [s for s in detail["spans"] if s["kind"] == "tool"]
    assert len(tool_spans) == 1
    assert tool_spans[0]["success"] == 1
