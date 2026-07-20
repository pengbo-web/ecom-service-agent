import itertools

from app.observability.store import TraceStore
from app.observability.tracer import Tracer
from app.observability.client_proxy import TracingClient


class _Usage:
    def __init__(self, p, c):
        self.prompt_tokens = p
        self.completion_tokens = c


class _Resp:
    def __init__(self, p, c):
        self.usage = _Usage(p, c)


class _FakeCompletions:
    def create(self, **kw):
        return _Resp(120, 30)


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()
        self.other_attr = "passthrough"


def _tracer(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    clock = itertools.count()
    ids = itertools.count(1)
    return Tracer(store, now=lambda: next(clock), id_factory=lambda: f"id{next(ids)}"), store


def test_create_records_llm_span_with_tokens(tmp_path):
    tracer, store = _tracer(tmp_path)
    client = TracingClient(_FakeClient(), tracer)
    with tracer.start_trace("s", "hi") as t:
        resp = client.chat.completions.create(model="x", messages=[])
        assert resp.usage.prompt_tokens == 120
    saved = store.get_trace(t.trace_id)
    llm = [s for s in saved["spans"] if s["kind"] == "llm"]
    assert len(llm) == 1
    assert saved["prompt_tokens"] == 120
    assert saved["completion_tokens"] == 30


def test_passthrough_other_attributes(tmp_path):
    tracer, _ = _tracer(tmp_path)
    client = TracingClient(_FakeClient(), tracer)
    assert client.other_attr == "passthrough"
