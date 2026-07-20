from app.observability.trace import Trace, Span
from app.observability.store import TraceStore


def _make_trace():
    spans = [
        Span("s1", "t1", "llm.chat.create", "llm", 1.0, 1.5, 500.0, None, 100, 20, {}),
        Span("s2", "t1", "tool:query_order", "tool", 1.5, 1.6, 100.0, True, 0, 0, {"name": "query_order"}),
    ]
    return Trace("t1", "sess1", "查订单", "order_query", 1.0, 1.7, 700.0, "ok", None, spans)


def test_trace_token_sums():
    t = _make_trace()
    assert t.prompt_tokens == 100
    assert t.completion_tokens == 20


def test_save_and_get_trace(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    store.save_trace(_make_trace())

    got = store.get_trace("t1")
    assert got["trace_id"] == "t1"
    assert got["intent"] == "order_query"
    assert len(got["spans"]) == 2
    assert got["prompt_tokens"] == 100


def test_recent_traces(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    store.save_trace(_make_trace())
    rows = store.recent_traces(limit=10)
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "t1"
    assert rows[0]["status"] == "ok"
