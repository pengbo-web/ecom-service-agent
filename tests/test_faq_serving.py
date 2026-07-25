"""FAQ 秒答接线:命中短路零LLM/未命中正常流/门控外问题不查缓存。"""

from types import SimpleNamespace

from app.agent.chat import EcomAgent
from app.agent.understanding import QueryUnderstanding
from app.config.settings import settings


def _agent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "faq_cache_enabled", True)
    agent = EcomAgent(session_path=str(tmp_path / "s.json"))
    agent.event_sink = agent_events.append
    monkeypatch.setattr(agent, "_write_snapshot", lambda: None)   # 隔离:不落真实快照库
    monkeypatch.setattr(agent, "_react_loop",
                        lambda: (_ for _ in ()).throw(AssertionError("LLM 不应被调用")))
    return agent


agent_events = []


def test_hit_short_circuits_llm(tmp_path, monkeypatch):
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr("app.agent.faq_cache.get_faq_cache",
                        lambda: SimpleNamespace(lookup=lambda q: {
                            "question": "下单后多久发货", "answer": "48小时内出库",
                            "score": 0.95}))
    agent.set_turn_understanding(QueryUnderstanding(
        intent="政策咨询", need_kb=True, kb_query="下单多久发货", source="llm"))
    result = agent.chat("下单多久能发货?")
    assert "48小时内出库" in result.reply and "依据《常见问题FAQ》" in result.reply
    assert result.confidence == 1.0
    ev = [e for e in agent_events if e["type"] == "faq_cache"][0]
    assert ev["matched"] == "下单后多久发货" and ev["score"] == 0.95
    # 会话历史照常落账:user + assistant 两条
    assert agent.raw_messages[-2]["role"] == "user"
    assert agent.raw_messages[-1]["role"] == "assistant"


def test_need_kb_false_skips_cache(tmp_path, monkeypatch):
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr("app.agent.faq_cache.get_faq_cache",
                        lambda: SimpleNamespace(lookup=lambda q: called.append(q)))
    agent.set_turn_understanding(QueryUnderstanding(
        intent="闲聊寒暄", need_kb=False, source="rule"))
    monkeypatch.setattr(agent, "_react_loop",
                        lambda: '{"intent":"other","confidence":0.9,"reply":"ok","requires_human":false}')
    monkeypatch.setattr(agent.memory_manager, "update_short_term", lambda *a, **k: None)
    monkeypatch.setattr(agent._reply_pipeline, "run", lambda *a, **k: a[3])
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None:
                        __import__("app.agent.recall.service", fromlist=["RecallResult"]).RecallResult())
    agent.chat("好的")
    assert called == []                              # 免检索轮不查缓存


def test_miss_falls_through_to_agent(tmp_path, monkeypatch):
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr("app.agent.faq_cache.get_faq_cache",
                        lambda: SimpleNamespace(lookup=lambda q: None))
    agent.set_turn_understanding(QueryUnderstanding(
        intent="政策咨询", need_kb=True, kb_query="冷门问题", source="llm"))
    monkeypatch.setattr(agent, "_react_loop",
                        lambda: '{"intent":"other","confidence":0.9,"reply":"正常回答","requires_human":false}')
    monkeypatch.setattr(agent.memory_manager, "update_short_term", lambda *a, **k: None)
    monkeypatch.setattr(agent._reply_pipeline, "run", lambda *a, **k: a[3])
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None:
                        __import__("app.agent.recall.service", fromlist=["RecallResult"]).RecallResult())
    result = agent.chat("冷门问题")
    assert "正常回答" in result.reply
    assert [e for e in agent_events if e["type"] == "faq_cache"] == []
