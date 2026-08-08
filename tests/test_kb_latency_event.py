"""任务③:ApeRAG 调用耗时/结果观测事件——走既有 tracer/Langfuse 通道
(自研 tracer + Langfuse 桥各自消费一次 `kb_latency` 事件),不新开通道。

覆盖点:
1. backend=aperag 且真的发起过一次调用时,才落一条 kb_latency 事件,带
   legs/duration_ms/outcome/rows;backend=local 或未检索时不落。
2. legs 反映 settings.aperag_fulltext_enabled(配置决定,不是从响应反解)。
3. 调用失败(aperag_search 返回 None,对应超时/连接拒/非200等任一种)时
   outcome="unavailable"、rows=0,且这一步本身不致命——_build_messages
   正常返回,回合能继续走完(任务②的"超时不致命"落到观测事件这一层)。
4. 并发预取路径(kb_prefetch 复用)一样能带出 meta——事件落在
   _build_messages 而不是后台线程,不受"谁真正发起了那次 HTTP 调用"影响。
5. 自研 tracer / Langfuse 桥都各自能正确消费这条新事件类型(不影响既有事件)。
"""

import json

from app.agent.chat import EcomAgent
from app.agent.understanding import QueryUnderstanding
from app.config.settings import settings
from app.observability.store import TraceStore
from app.observability.tracer import Tracer


def _agent():
    return EcomAgent(session_path="app/sessions/_test_kb_latency_event.json")


def _kb_events(events):
    return [e for e in events if e["type"] == "kb_latency"]


def test_kb_latency_emitted_on_aperag_success(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr(settings, "aperag_fulltext_enabled", False)
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search",
                        lambda q: [{"doc": "退换货政策", "section": "vector_search",
                                    "score": 0.9, "text": "命中"}])
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "退货政策是什么"})
    agent._turn_recall = None
    agent._build_messages()

    kb_events = _kb_events(events)
    assert len(kb_events) == 1
    ev = kb_events[0]
    assert ev["backend"] == "aperag"
    assert ev["outcome"] == "ok"
    assert ev["rows"] == 1
    assert ev["legs"] == ["vector"]
    assert isinstance(ev["duration_ms"], float) and ev["duration_ms"] >= 0


def test_kb_latency_legs_include_fulltext_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr(settings, "aperag_fulltext_enabled", True)
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search", lambda q: [])
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "退货政策是什么"})
    agent._turn_recall = None
    agent._build_messages()

    ev = _kb_events(events)[0]
    assert ev["legs"] == ["vector", "fulltext"]
    assert ev["outcome"] == "ok" and ev["rows"] == 0   # 正常无命中,不是失败


def test_kb_latency_emitted_on_aperag_unavailable_and_turn_still_completes(monkeypatch):
    """任务②约束的落地证明:aperag_search 返回 None(超时/连接拒/非200等
    都收敛成这一种)时,outcome=unavailable、rows=0,且 _build_messages
    本身不抛异常——这一轮拿不到知识库注入,但回合本身能继续走完。"""
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr(settings, "kb_local_fallback_enabled", False)
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search", lambda q: None)
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "退货政策是什么"})
    agent._turn_recall = None
    messages = agent._build_messages()   # 不抛异常 = 回合能继续

    ev = _kb_events(events)[0]
    assert ev["outcome"] == "unavailable"
    assert ev["rows"] == 0
    # 没有知识库注入段,但 messages 本身仍然是合法的、可以送进生成的列表
    assert not any("平台知识" in m.get("content", "") for m in messages)


def test_no_kb_latency_event_on_local_backend(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "local")
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "退货政策是什么"})
    agent._turn_recall = None
    agent._build_messages()
    assert _kb_events(events) == []


def test_no_kb_latency_event_when_kb_gate_skips_before_ever_calling_aperag(monkeypatch):
    """recall_kb_enabled=False:kb_fetch_rows 早退,连 aperag 都没碰——不该
    虚报一次没发生的调用。"""
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr(settings, "recall_kb_enabled", False)
    called = []
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search",
                        lambda q: called.append(q))
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "退货政策是什么"})
    agent._turn_recall = None
    agent._build_messages()
    assert called == []
    assert _kb_events(events) == []


def test_kb_latency_carried_through_concurrent_prefetch_path(monkeypatch):
    """并发预取(orchestrator 后台线程取到 (rows, backend, meta)，经 Future
    传回)一样要能带出 kb_latency——事件本身落在 _build_messages(主线程,
    正确的 tracer/Langfuse 上下文),不依赖"谁真正发起了那次 HTTP 调用"。"""
    import concurrent.futures

    monkeypatch.setattr(settings, "kb_backend", "aperag")
    rows = [{"doc": "d", "section": "s", "score": 0.9, "text": "t"}]
    meta = {"legs": ["vector"], "duration_ms": 42.0, "outcome": "ok", "rows": 1}
    f = concurrent.futures.Future()
    f.set_result((rows, "aperag", meta))

    agent = _agent()
    agent.set_turn_understanding(QueryUnderstanding(
        domain="aftersale", intent="政策咨询", need_kb=True,
        kb_query="退货政策是什么", source="llm"))
    agent.set_turn_kb_prefetch_future("退货政策是什么", f)
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "退货政策是什么"})
    agent._turn_recall = None
    agent._build_messages()

    ev = _kb_events(events)[0]
    assert ev == {"type": "kb_latency", "backend": "aperag",
                  "legs": ["vector"], "duration_ms": 42.0,
                  "outcome": "ok", "rows": 1}


def test_tracer_records_kb_latency_span(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    tracer = Tracer(store)
    with tracer.start_trace("sess1", "退货政策") as t:
        tracer.on_event({"type": "kb_latency", "backend": "aperag",
                         "legs": ["vector"], "duration_ms": 123.0,
                         "outcome": "ok", "rows": 2})
    saved = store.get_trace(t.trace_id)
    spans = [s for s in saved["spans"] if s["kind"] == "kb_latency"]
    assert len(spans) == 1
    assert spans[0]["latency_ms"] == 123.0
    meta = json.loads(spans[0]["meta"])
    assert meta == {"backend": "aperag", "legs": ["vector"], "rows": 2, "outcome": "ok"}


def test_langfuse_bridge_dispatches_kb_latency_without_error(monkeypatch):
    """门控开、SDK 未装/桩过的情况下 on_event 不该抛异常——与其它事件类型
    同一姿态的 best-effort 验证(不断言具体 SDK 调用细节)。"""
    from app.observability.langfuse_bridge import _LangfuseTurn

    calls = []

    class _FakeObs:
        def end(self):
            calls.append("end")

    class _FakeClient:
        def start_observation(self, **kwargs):
            calls.append(kwargs)
            return _FakeObs()

        def start_as_current_observation(self, **kwargs):
            class _Cm:
                def __enter__(self_inner):
                    return _FakeObs()

                def __exit__(self_inner, *a):
                    return False
            return _Cm()

    turn = _LangfuseTurn(_FakeClient(), "sess1", "u1", "退货政策")
    turn._root = object()   # 绕开 __enter__,只测 _dispatch
    turn.on_event({"type": "kb_latency", "backend": "aperag", "legs": ["vector"],
                  "duration_ms": 50.0, "outcome": "timeout", "rows": 0})
    assert any(isinstance(c, dict) and c.get("name") == "kb_latency" for c in calls)
    assert any(isinstance(c, dict) and c.get("level") == "WARNING" for c in calls)
