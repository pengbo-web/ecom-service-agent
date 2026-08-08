"""FAQ 秒答接线:命中短路零LLM/未命中正常流/门控外问题不查缓存/三态可观测/
子系统不可用时买家这一轮仍能走完(W1 L1)。"""

from types import SimpleNamespace

import pytest

from app.agent.chat import EcomAgent
from app.agent.faq_cache import FaqLookupOutcome
from app.agent.rag.errors import EmbeddingIndexMismatchError
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


def _stub_recall_and_llm(agent, monkeypatch, reply_text: str):
    """走到正常 Agent 流程时,把 react_loop 之后的重活桩掉,只关心 FAQ 缓存这一段接线。"""
    monkeypatch.setattr(agent, "_react_loop", lambda: (
        '{"intent":"other","confidence":0.9,"reply":"%s","requires_human":false}' % reply_text
    ))
    monkeypatch.setattr(agent.memory_manager, "update_short_term", lambda *a, **k: None)
    monkeypatch.setattr(agent._reply_pipeline, "run", lambda *a, **k: a[3])
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None:
                        __import__("app.agent.recall.service", fromlist=["RecallResult"]).RecallResult())


agent_events = []


def test_hit_short_circuits_llm(tmp_path, monkeypatch):
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.agent.faq_cache.get_faq_cache",
        lambda: SimpleNamespace(lookup_with_state=lambda q: FaqLookupOutcome(
            state="hit", hit={"question": "下单后多久发货", "answer": "48小时内出库", "score": 0.95})))
    agent.set_turn_understanding(QueryUnderstanding(
        intent="政策咨询", need_kb=True, kb_query="下单多久发货", source="llm"))
    result = agent.chat("下单多久能发货?")
    assert "48小时内出库" in result.reply and "依据《常见问题FAQ》" in result.reply
    assert result.confidence == 1.0
    ev = [e for e in agent_events if e["type"] == "faq_cache"][0]
    assert ev["state"] == "hit"
    assert ev["matched"] == "下单后多久发货" and ev["score"] == 0.95
    # 会话历史照常落账:user + assistant 两条
    assert agent.raw_messages[-2]["role"] == "user"
    assert agent.raw_messages[-1]["role"] == "assistant"


def test_need_kb_false_skips_cache(tmp_path, monkeypatch):
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(
        "app.agent.faq_cache.get_faq_cache",
        lambda: SimpleNamespace(lookup_with_state=lambda q: called.append(q)))
    agent.set_turn_understanding(QueryUnderstanding(
        intent="闲聊寒暄", need_kb=False, source="rule"))
    _stub_recall_and_llm(agent, monkeypatch, "ok")
    agent.chat("好的")
    assert called == []                              # 免检索轮不查缓存,连 get_faq_cache 都不碰
    assert [e for e in agent_events if e["type"] == "faq_cache"] == []


def test_miss_falls_through_to_agent(tmp_path, monkeypatch):
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.agent.faq_cache.get_faq_cache",
        lambda: SimpleNamespace(lookup_with_state=lambda q: FaqLookupOutcome(state="miss")))
    agent.set_turn_understanding(QueryUnderstanding(
        intent="政策咨询", need_kb=True, kb_query="冷门问题", source="llm"))
    _stub_recall_and_llm(agent, monkeypatch, "正常回答")
    result = agent.chat("冷门问题")
    assert "正常回答" in result.reply
    # W1 L1:miss 现在也要有埋点(过去只在命中时才发,miss/unavailable 分不清)
    ev = [e for e in agent_events if e["type"] == "faq_cache"]
    assert len(ev) == 1 and ev[0]["state"] == "miss"


def test_unavailable_falls_through_to_agent_and_is_marked(tmp_path, monkeypatch):
    """三态之三:embedding 调用失败(子系统不可用)。买家这一轮必须仍然拿到
    正常回复(fail-soft 不因为观测层改动而改变),但 trace 里要能看到
    state=unavailable + 错误摘要,不能再跟 miss 长得一样。"""
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.agent.faq_cache.get_faq_cache",
        lambda: SimpleNamespace(lookup_with_state=lambda q: FaqLookupOutcome(
            state="unavailable", error="404 model_not_found")))
    agent.set_turn_understanding(QueryUnderstanding(
        intent="政策咨询", need_kb=True, kb_query="退货政策", source="llm"))
    _stub_recall_and_llm(agent, monkeypatch, "正常回答")
    result = agent.chat("退货政策是什么")
    assert "正常回答" in result.reply             # 买家这一轮仍然成功
    ev = [e for e in agent_events if e["type"] == "faq_cache"]
    assert len(ev) == 1
    assert ev[0]["state"] == "unavailable"
    assert ev[0]["error"] == "404 model_not_found"


def test_lookup_unexpected_exception_is_treated_as_unavailable_not_crash(tmp_path, monkeypatch):
    """lookup_with_state 本身意外抛出(非 EmbeddingIndexMismatchError)时,
    chat() 兜底成 unavailable 继续走完这一轮,不能让买家看见 500。"""
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)

    def _boom(q):
        raise RuntimeError("网络抖动")

    monkeypatch.setattr(
        "app.agent.faq_cache.get_faq_cache",
        lambda: SimpleNamespace(lookup_with_state=_boom))
    agent.set_turn_understanding(QueryUnderstanding(
        intent="政策咨询", need_kb=True, kb_query="退货政策", source="llm"))
    _stub_recall_and_llm(agent, monkeypatch, "正常回答")
    result = agent.chat("退货政策是什么")
    assert "正常回答" in result.reply
    ev = [e for e in agent_events if e["type"] == "faq_cache"][0]
    assert ev["state"] == "unavailable"


def test_index_mismatch_propagates_not_swallowed(tmp_path, monkeypatch):
    """维度/模型不匹配是本任务里唯一"故意不 fail-soft"的例外——它在
    get_faq_cache() 这一层(单例首次构造)抛出,chat() 不得把它当成常规的
    embedding 失败吞掉,必须原样往外抛(由更上层的 SSE 兜底转成可见的
    error 事件,而不是伪装成一次平静的未命中)。"""
    agent_events.clear()
    agent = _agent(tmp_path, monkeypatch)

    def _raise_mismatch():
        raise EmbeddingIndexMismatchError("索引模型与当前配置不一致,请重建")

    monkeypatch.setattr("app.agent.faq_cache.get_faq_cache", _raise_mismatch)
    agent.set_turn_understanding(QueryUnderstanding(
        intent="政策咨询", need_kb=True, kb_query="退货政策", source="llm"))
    with pytest.raises(EmbeddingIndexMismatchError):
        agent.chat("退货政策是什么")
