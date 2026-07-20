from app.observability.store import TraceStore
from app.observability.trace import Trace, Span
from app.observability.metrics import compute_metrics


def _seed(store):
    store.save_trace(Trace(
        "t1", "s", "查订单", "order_query", 0.0, 0.2, 200.0, "ok", None,
        [Span("a", "t1", "llm.chat.create", "llm", 0, 0.1, 100, None, 100, 20, {}),
         Span("b", "t1", "tool:query_order", "tool", 0.1, 0.2, 100, True, 0, 0, {})],
    ))
    store.save_trace(Trace(
        "t2", "s", "退货", "return_request", 1.0, 1.6, 600.0, "error", "boom",
        [Span("c", "t2", "tool:apply_refund", "tool", 1.0, 1.1, 100, False, 0, 0, {})],
    ))


def test_compute_metrics(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    _seed(store)
    m = compute_metrics(store)
    assert m["total_traces"] == 2
    assert m["error_rate"] == 0.5
    assert m["tool_calls"] == 2
    assert m["tool_success_rate"] == 0.5           # 1 成功 / 2
    assert m["total_prompt_tokens"] == 100
    assert m["intent_distribution"]["order_query"] == 1
    assert m["latency_p50_ms"] >= 0
