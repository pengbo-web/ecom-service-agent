"""chat 装配接线:召回段注入/每轮只检索一次/recall 事件只发一次/tracer 落 span。"""

import itertools
import json

from app.agent.chat import EcomAgent
from app.agent.recall.service import RecallResult
from app.observability.store import TraceStore
from app.observability.tracer import Tracer


def _agent():
    # 构造不触发网络调用(与 tests/test_emit.py 同模式)
    return EcomAgent(session_path="app/sessions/_test_recall_wiring.json")


def _fake_recall(calls):
    def fake(mm, query, include_kb=True, kb_domain=None, kb_prefetch=None):
        calls.append(query)
        return RecallResult(
            sections=[{"role": "system", "content": "【平台知识(自动检索)】测试片段"}],
            kb_hits=[{"doc": "退换货政策", "section": "七天无理由", "score": 0.9}],
        )
    return fake


def test_build_messages_injects_and_caches_per_turn(monkeypatch):
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
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None: RecallResult())
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.raw_messages.append({"role": "user", "content": "你好"})
    agent._turn_recall = None
    agent._build_messages()
    assert [e for e in events if e["type"] == "recall"] == []


def test_recall_uses_qu_kb_query_and_event_carries_it(monkeypatch):
    """QU 给出的自包含查询喂给召回,事件带实际检索查询。"""
    from app.agent.understanding import QueryUnderstanding
    calls = []

    def fake_recall(mm, query, include_kb=True, kb_domain=None, kb_prefetch=None):
        calls.append((query, include_kb))
        return RecallResult(
            sections=[{"role": "system", "content": "【平台知识(自动检索)】X"}],
            kb_hits=[{"doc": "d", "section": "s", "score": 0.9}],
        )

    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", fake_recall)
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.set_turn_understanding(QueryUnderstanding(
        domain="aftersale", intent="政策咨询", need_kb=True,
        kb_query="退货运费谁承担", source="llm"))
    agent.raw_messages.append({"role": "user", "content": "那运费呢?"})
    agent._turn_recall = None
    agent._build_messages()
    assert calls == [("退货运费谁承担", True)]
    ev = [e for e in events if e["type"] == "recall"][0]
    assert ev["query"] == "退货运费谁承担" and not ev.get("skipped")


def test_qu_need_kb_false_skips_and_emits_skipped(monkeypatch):
    """门控关检索:include_kb=False 传入召回,发 skipped 事件。"""
    from app.agent.understanding import QueryUnderstanding
    calls = []

    def fake_recall(mm, query, include_kb=True, kb_domain=None, kb_prefetch=None):
        calls.append((query, include_kb))
        return RecallResult(kb_backend="skipped")

    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", fake_recall)
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.set_turn_understanding(QueryUnderstanding(
        intent="闲聊寒暄", need_kb=False, source="rule"))
    agent.raw_messages.append({"role": "user", "content": "好的"})
    agent._turn_recall = None
    agent._build_messages()
    assert calls == [("好的", False)]          # 记忆查询仍用原句
    ev = [e for e in events if e["type"] == "recall"][0]
    assert ev["skipped"] is True and ev["reason"] == "闲聊寒暄"


def test_qu_need_kb_true_no_hits_emits_nothing(monkeypatch):
    """qu 存在且要检索但无命中:既不发正常事件也不发 skipped(防 elif 被改破)。"""
    from app.agent.understanding import QueryUnderstanding
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None: RecallResult())
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent.set_turn_understanding(QueryUnderstanding(
        intent="政策咨询", need_kb=True, kb_query="政策", source="llm"))
    agent.raw_messages.append({"role": "user", "content": "某政策"})
    agent._turn_recall = None
    agent._build_messages()
    assert [e for e in events if e["type"] == "recall"] == []


def test_no_qu_defaults_to_old_behavior(monkeypatch):
    """引擎独立运行(无 orchestrator 注入 QU):原句检索,include_kb=True。"""
    calls = []

    def fake_recall(mm, query, include_kb=True, kb_domain=None, kb_prefetch=None):
        calls.append((query, include_kb))
        return RecallResult()

    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", fake_recall)
    agent = _agent()
    agent.event_sink = lambda e: None
    agent.raw_messages.append({"role": "user", "content": "退货政策"})
    agent._turn_recall = None
    agent._build_messages()
    assert calls == [("退货政策", True)]


def test_chat_passes_qu_domain_to_recall(monkeypatch):
    from app.agent.understanding import QueryUnderstanding
    captured = {}

    def fake_recall(mm, query, include_kb=True, kb_domain=None, kb_prefetch=None):
        captured.update(query=query, kb_domain=kb_domain)
        return RecallResult()

    monkeypatch.setattr("app.agent.recall.service.build_recall_sections", fake_recall)
    agent = _agent()
    agent.event_sink = lambda e: None
    agent.set_turn_understanding(QueryUnderstanding(
        domain="aftersale", intent="政策咨询", need_kb=True,
        kb_query="退货运费", source="llm"))
    agent.raw_messages.append({"role": "user", "content": "运费"})
    agent._turn_recall = None
    agent._build_messages()
    assert captured["kb_domain"] == "aftersale"


def test_tracer_records_recall_span(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    clock = itertools.count(start=0, step=1)
    ids = itertools.count(start=1)
    tracer = Tracer(store, now=lambda: next(clock), id_factory=lambda: f"id{next(ids)}")
    with tracer.start_trace("sess1", "退货政策") as t:
        tracer.on_event({"type": "recall", "source": "kb",
                         "query": "退货运费谁承担",
                         "hits": [{"doc": "退换货政策", "section": "七天无理由", "score": 0.62}]})
    saved = store.get_trace(t.trace_id)
    recall_spans = [s for s in saved["spans"] if s["kind"] == "recall"]
    assert len(recall_spans) == 1
    assert recall_spans[0]["name"] == "recall:kb"
    # 同 test_kb_latency_event:`get_trace()` 已解析 meta,`all_spans()` 才是字符串。
    raw = recall_spans[0]["meta"]
    meta = json.loads(raw) if isinstance(raw, str) else raw
    assert meta["query"] == "退货运费谁承担"          # 检索查询要能在 tracer 里看到
