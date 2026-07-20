import json
import itertools

from app.observability.store import TraceStore
from app.observability.tracer import Tracer


def _tracer(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    clock = itertools.count(start=0, step=1)          # 确定性时钟：0,1,2,...
    ids = itertools.count(start=1)
    return Tracer(store, now=lambda: next(clock),
                  id_factory=lambda: f"id{next(ids)}"), store


def test_start_trace_persists_on_exit(tmp_path):
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "你好") as t:
        t.intent = "greeting"
    saved = store.get_trace(t.trace_id)
    assert saved is not None
    assert saved["status"] == "ok"
    assert saved["intent"] == "greeting"
    assert saved["latency_ms"] >= 0


def test_span_attached_to_trace(tmp_path):
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "hi") as t:
        with tracer.span("llm.chat.create", "llm") as sp:
            sp.prompt_tokens = 50
            sp.completion_tokens = 10
    saved = store.get_trace(t.trace_id)
    assert len(saved["spans"]) == 1
    assert saved["spans"][0]["kind"] == "llm"
    assert saved["prompt_tokens"] == 50


def test_on_event_pairs_tool_spans(tmp_path):
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "查订单") as t:
        tracer.on_event({"type": "tool_call", "name": "query_order", "args": {"id": "A"}})
        tracer.on_event({"type": "tool_result",
                         "content": json.dumps({"success": True})})
    saved = store.get_trace(t.trace_id)
    tool_spans = [s for s in saved["spans"] if s["kind"] == "tool"]
    assert len(tool_spans) == 1
    assert tool_spans[0]["name"] == "tool:query_order"
    assert tool_spans[0]["success"] == 1


def test_error_status_recorded(tmp_path):
    tracer, store = _tracer(tmp_path)
    try:
        with tracer.start_trace("sess1", "boom") as t:
            raise RuntimeError("炸了")
    except RuntimeError:
        pass
    saved = store.get_trace(t.trace_id)
    assert saved["status"] == "error"
    assert "炸了" in saved["error"]
