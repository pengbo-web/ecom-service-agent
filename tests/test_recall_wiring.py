"""chat 装配接线:召回段注入/每轮只检索一次/recall 事件只发一次/tracer 落 span。"""

import itertools

from app.agent.chat import EcomAgent
from app.agent.recall.service import RecallResult
from app.observability.store import TraceStore
from app.observability.tracer import Tracer


def _agent():
    # 构造不触发网络调用(与 tests/test_emit.py 同模式)
    return EcomAgent(session_path="app/sessions/_test_recall_wiring.json")


def _fake_recall(calls):
    def fake(mm, query):
        calls.append(query)
        return RecallResult(
            sections=[{"role": "system", "content": "【平台知识(自动检索)】测试片段"}],
            kb_hits=[{"doc": "退换货政策", "section": "七天无理由", "score": 0.9}],
        )
    return fake


def test_build_messages_injects_and_caches_per_turn(monkeypatch):
    monkeypatch.setattr("app.agent.recall.rewrite.rewrite_for_recall",
                        lambda client, model, messages, q: q)
    calls = []
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", _fake_recall(calls))
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "退货政策是什么"})
    agent._turn_recall = None

    m1 = agent._build_messages()
    m2 = agent._build_messages()      # 模拟 react 第二步:必须复用缓存

    assert any(msg["role"] == "system" and "测试片段" in msg["content"] for msg in m1)
    assert any(msg["role"] == "system" and "测试片段" in msg["content"] for msg in m2)
    assert calls == ["退货政策是什么"]                       # 整轮只检索一次
    recall_events = [e for e in events if e["type"] == "recall"]
    assert len(recall_events) == 1                           # 事件也只发一次
    assert recall_events[0]["source"] == "kb"
    assert recall_events[0]["hits"][0]["doc"] == "退换货政策"


def test_new_user_turn_recomputes(monkeypatch):
    monkeypatch.setattr("app.agent.recall.rewrite.rewrite_for_recall",
                        lambda client, model, messages, q: q)
    calls = []
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", _fake_recall(calls))
    agent = _agent()
    agent.event_sink = lambda e: None
    agent.raw_messages.append({"role": "user", "content": "第一问"})
    agent._turn_recall = None
    agent._build_messages()
    agent.raw_messages.append({"role": "user", "content": "第二问"})
    agent._build_messages()
    assert calls == ["第一问", "第二问"]                      # last_user 变了要重算


def test_no_hits_no_event(monkeypatch):
    monkeypatch.setattr("app.agent.recall.rewrite.rewrite_for_recall",
                        lambda client, model, messages, q: q)
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q: RecallResult())
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "你好"})
    agent._turn_recall = None
    agent._build_messages()
    assert [e for e in events if e["type"] == "recall"] == []


def test_recall_uses_rewritten_query_and_event_carries_it(monkeypatch):
    calls = []

    def fake_recall(mm, query):
        calls.append(query)
        return RecallResult(
            sections=[{"role": "system", "content": "【平台知识(自动检索)】X"}],
            kb_hits=[{"doc": "d", "section": "s", "score": 0.9}],
        )

    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", fake_recall)
    monkeypatch.setattr("app.agent.recall.rewrite.rewrite_for_recall",
                        lambda client, model, messages, q: "改写后的自包含查询")
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "那运费呢?"})
    agent._turn_recall = None
    agent._build_messages()
    assert calls == ["改写后的自包含查询"]            # 召回吃的是改写后查询
    ev = [e for e in events if e["type"] == "recall"][0]
    assert ev["query"] == "改写后的自包含查询"        # 事件带上实际检索查询


def test_tracer_records_recall_span(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    clock = itertools.count(start=0, step=1)
    ids = itertools.count(start=1)
    tracer = Tracer(store, now=lambda: next(clock), id_factory=lambda: f"id{next(ids)}")
    with tracer.start_trace("sess1", "退货政策") as t:
        tracer.on_event({"type": "recall", "source": "kb",
                         "hits": [{"doc": "退换货政策", "section": "七天无理由", "score": 0.62}]})
    saved = store.get_trace(t.trace_id)
    recall_spans = [s for s in saved["spans"] if s["kind"] == "recall"]
    assert len(recall_spans) == 1
    assert recall_spans[0]["name"] == "recall:kb"
