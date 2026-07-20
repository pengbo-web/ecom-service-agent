from app.observability.store import TraceStore
from app.observability.trace import Trace, Span
from app.observability.metrics import compute_metrics


def test_guard_metrics(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    store.save_trace(Trace(
        "t1", "s", "x", "blocked", 0.0, 0.1, 100.0, "ok", None,
        [Span("g1", "t1", "guard:prompt_injection", "guard", 0, 0, 0, None, 0, 0,
              {"stage": "input", "action": "block"})],
    ))
    store.save_trace(Trace(
        "t2", "s", "y", "order_query", 1.0, 1.2, 200.0, "ok", None,
        [Span("g2", "t2", "guard:sensitive_info", "guard", 1, 1, 0, None, 0, 0,
              {"stage": "output", "action": "sanitize"})],
    ))
    m = compute_metrics(store)
    assert m["guard_blocks"] == 1
    assert m["guard_sanitizes"] == 1
    assert m["block_rate"] == 0.5      # 1 拦截 / 2 请求
