"""orchestrator 接入查询理解:路由来源/粘性/事件增强/回滚路径。"""

from app.agent.recall.service import RecallResult
from app.agent.understanding import QueryUnderstanding
from app.config.settings import settings
from app.multi_agent.orchestrator import MultiAgentOrchestrator


def _orch(tmp_path, monkeypatch):
    o = MultiAgentOrchestrator(session_path=str(tmp_path / "s.json"), user_id="u1")
    # 引擎不触网:react 返回固定结构化文本;流水线原样透传;记忆更新打桩;
    # 召回打桩(chat 轮末 token 估算会真调 _build_messages→召回,不桩会打真网络)
    o.engine._react_loop = lambda: '{"intent":"other","confidence":0.9,"reply":"ok","requires_human":false}'
    o.engine.memory_manager.update_short_term = lambda *a, **k: None
    o.engine._reply_pipeline.run = lambda *a, **k: a[3]   # 原样返回草稿,不走流水线
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True: RecallResult())
    return o


def test_qu_domain_routes_and_event_enriched(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    qu = QueryUnderstanding(domain="presale", intent="商品咨询",
                            need_kb=False, source="llm")
    monkeypatch.setattr("app.agent.understanding.understand", lambda *a, **k: qu)
    o = _orch(tmp_path, monkeypatch)
    events = []
    o.event_sink = events.append
    o.chat("这双鞋怎么样")
    route = [e for e in events if e["type"] == "route"][0]
    assert route["key"] == "presale"
    assert route["intent"] == "商品咨询" and route["need_kb"] is False and route["source"] == "llm"
    assert o.engine._turn_qu is qu                        # QU 注入了引擎


def test_domain_none_sticky_to_last_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    seq = [QueryUnderstanding(domain="midsale", intent="订单事务", need_kb=False, source="llm"),
           QueryUnderstanding(domain=None, intent="闲聊寒暄", need_kb=False, source="rule")]
    monkeypatch.setattr("app.agent.understanding.understand",
                        lambda *a, **k: seq.pop(0))
    o = _orch(tmp_path, monkeypatch)
    events = []
    o.event_sink = events.append
    o.chat("查一下订单")
    o.chat("好的")
    routes = [e for e in events if e["type"] == "route"]
    assert routes[0]["key"] == "midsale"
    assert routes[1]["key"] == "midsale"                  # 粘住上一轮,不跳默认域


def test_domain_none_first_turn_uses_default(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    monkeypatch.setattr("app.agent.understanding.understand",
                        lambda *a, **k: QueryUnderstanding(domain=None, intent="其他",
                                                           need_kb=True, kb_query="x",
                                                           source="fallback"))
    o = _orch(tmp_path, monkeypatch)
    events = []
    o.event_sink = events.append
    o.chat("嗯?")
    assert [e for e in events if e["type"] == "route"][0]["key"] == "aftersale"


def test_disabled_falls_back_to_router(tmp_path, monkeypatch):
    """回滚路径:关开关走老 Router,事件保持旧形态,引擎 QU 为 None。"""
    monkeypatch.setattr(settings, "query_understanding_enabled", False)
    o = _orch(tmp_path, monkeypatch)
    monkeypatch.setattr(o.router, "route", lambda *a, **k: "aftersale")
    events = []
    o.event_sink = events.append
    o.chat("退货")
    route = [e for e in events if e["type"] == "route"][0]
    assert route["key"] == "aftersale" and "intent" not in route
    assert o.engine._turn_qu is None
