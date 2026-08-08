"""L3③:生成前进度事件——如实反映当前阶段(理解/检索/生成),新增帧不影响既有帧。"""

import concurrent.futures

from app.agent.chat import EcomAgent
from app.agent.progress import ProgressStageGate, stage_message
from app.agent.recall.service import RecallResult
from app.agent.understanding import QueryUnderstanding
from app.config.settings import settings
from app.multi_agent.orchestrator import MultiAgentOrchestrator


def _agent():
    return EcomAgent(session_path="app/sessions/_test_progress_events.json")


def _resolved_future(value):
    f = concurrent.futures.Future()
    f.set_result(value)
    return f


def _orch(tmp_path, monkeypatch, name="s.json"):
    o = MultiAgentOrchestrator(session_path=str(tmp_path / name), user_id="u1")
    o.engine._react_loop = lambda: '{"intent":"other","confidence":0.9,"reply":"ok","requires_human":false}'
    o.engine.memory_manager.update_short_term = lambda *a, **k: None
    o.engine._reply_pipeline.run = lambda *a, **k: a[3]
    return o


# ---- 引擎侧:generating / retrieving 阶段 ----

def test_generating_progress_emitted_before_react_loop(monkeypatch):
    """L3③ Defect-1 修复后:"generating" 是在 `_react_loop` 第 0 步、
    `_build_messages()` 返回之后才发的(不再是 chat() 里无条件先发),这里
    不能再整体桩掉 `_react_loop`(会跳过发这条事件的那行代码)——改成桩掉
    更底层的 `_llm_create`(与 test_react_degrade.py 同一层级),让真实的
    `_react_loop`/`_build_messages` 跑起来。"""
    monkeypatch.setattr(settings, "progress_events_enabled", True)
    agent = _agent()
    fake_msg = type("M", (), {"content": "ok", "tool_calls": None})()
    fake_resp = type("R", (), {"choices": [type("C", (), {"message": fake_msg})()]})()
    monkeypatch.setattr(agent, "_llm_create", lambda messages, use_tools: fake_resp)
    agent.memory_manager.update_short_term = lambda *a, **k: None
    agent._reply_pipeline.run = lambda *a, **k: a[3]
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None: RecallResult())
    events = []
    agent.event_sink = events.append
    agent.chat("你好呀")
    progress = [e for e in events if e["type"] == "progress"]
    assert any(p["stage"] == "generating" and p["message"] == "正在为您生成回复…" for p in progress)


def test_no_generating_progress_when_faq_cache_hits(monkeypatch):
    """FAQ 秒答分支从没到过 react 循环,不该出现"正在生成"这条——那是谎话。"""
    monkeypatch.setattr(settings, "progress_events_enabled", True)
    monkeypatch.setattr(settings, "faq_cache_enabled", True)
    agent = _agent()
    agent.set_turn_understanding(QueryUnderstanding(need_kb=True, kb_query="价保多久", source="rule"))

    from app.agent.faq_cache import FaqLookupOutcome
    import app.agent.faq_cache as faq_mod

    class _FakeCache:
        def lookup_with_state(self, q):
            return FaqLookupOutcome(state="hit", hit={"question": "价保多久", "answer": "7天", "score": 0.95})
    monkeypatch.setattr(faq_mod, "get_faq_cache", lambda: _FakeCache())

    events = []
    agent.event_sink = events.append
    agent.chat("价保多久")
    progress = [e for e in events if e["type"] == "progress"]
    assert not any(p["stage"] == "generating" for p in progress)


def test_retrieving_progress_emitted_when_real_fetch_about_to_happen(monkeypatch):
    monkeypatch.setattr(settings, "progress_events_enabled", True)
    agent = _agent()
    agent.set_turn_understanding(QueryUnderstanding(
        domain="midsale", intent="订单事务", need_kb=True, kb_query="订单在哪", source="llm"))
    agent.raw_messages.append({"role": "user", "content": "订单在哪"})
    agent._turn_recall = None
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None: RecallResult())
    events = []
    agent.event_sink = events.append
    agent._build_messages()
    progress = [e for e in events if e["type"] == "progress" and e["stage"] == "retrieving"]
    assert len(progress) == 1
    assert progress[0]["message"] == "正在为您查询订单和物流信息…"   # domain=midsale


def test_retrieving_progress_skipped_when_prefetch_already_resolved(monkeypatch):
    """预取已经命中复用,这一刻检索早就做完了,不该再说"正在检索"(会是假话)。"""
    monkeypatch.setattr(settings, "progress_events_enabled", True)
    agent = _agent()
    agent.set_turn_understanding(QueryUnderstanding(
        domain="aftersale", intent="政策咨询", need_kb=True, kb_query="退货政策是什么", source="llm"))
    agent.set_turn_kb_prefetch_future(
        "退货政策是什么",
        _resolved_future(([{"doc": "d", "section": "s", "score": 0.9, "text": "t"}], "local")))
    agent.raw_messages.append({"role": "user", "content": "退货政策是什么"})
    agent._turn_recall = None
    events = []
    agent.event_sink = events.append
    agent._build_messages()
    assert [e for e in events if e["type"] == "progress" and e["stage"] == "retrieving"] == []


def test_switch_off_emits_no_progress_events(monkeypatch):
    monkeypatch.setattr(settings, "progress_events_enabled", False)
    agent = _agent()
    agent._react_loop = lambda: '{"intent":"other","confidence":0.9,"reply":"ok","requires_human":false}'
    agent.memory_manager.update_short_term = lambda *a, **k: None
    agent._reply_pipeline.run = lambda *a, **k: a[3]
    agent.set_turn_understanding(QueryUnderstanding(
        domain="midsale", intent="订单事务", need_kb=True, kb_query="订单在哪", source="llm"))
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None: RecallResult())
    events = []
    agent.event_sink = events.append
    agent.chat("订单在哪")
    assert [e for e in events if e["type"] == "progress"] == []


# ---- Defect-1 回归:阶段单调门(不能倒退) ----

def test_stage_gate_suppresses_regression():
    """实测发现的真实回归复现:generating(rank2)之后又来一条 retrieving
    (rank1)——单调门必须吞掉这条倒退帧,不能让买家看到"退回上一阶段"。"""
    sent = []
    gate = ProgressStageGate()
    gate.emit(sent.append, "understanding")
    gate.emit(sent.append, "retrieving")
    gate.emit(sent.append, "generating")
    gate.emit(sent.append, "retrieving")   # 倒退,必须被吞
    stages = [e["stage"] for e in sent]
    assert stages == ["understanding", "retrieving", "generating"]
    # 单调性本身的显式断言:任意相邻两条已发出的帧,阶段号不能变小。
    from app.agent.progress import STAGE_RANK
    ranks = [STAGE_RANK[s] for s in stages]
    assert ranks == sorted(ranks), "已发出的进度帧必须单调不减,不能出现倒退"


def test_stage_gate_allows_same_rank_repeat():
    """同一阶段重复发(如两次 retrieving)不算倒退,不该被吞。"""
    sent = []
    gate = ProgressStageGate()
    gate.emit(sent.append, "retrieving", "presale")
    gate.emit(sent.append, "retrieving", "aftersale")
    assert [e["stage"] for e in sent] == ["retrieving", "retrieving"]


def test_stage_gate_reset_starts_fresh():
    gate = ProgressStageGate()
    sent = []
    gate.emit(sent.append, "generating")
    gate.reset()
    gate.emit(sent.append, "understanding")   # reset 后不该被当成"倒退"吞掉
    assert [e["stage"] for e in sent] == ["generating", "understanding"]


def test_engine_emit_progress_never_regresses_across_a_turn(monkeypatch):
    """端到端复现 Defect-1 的实际触发路径:通过引擎的 `_emit_progress`(不是
    裸调 gate)注入一次"事后才发现该走 retrieving"的场景,验证同一轮内序列
    仍然单调——即使调用方按错误顺序调用,买家也不会看到倒退帧。"""
    monkeypatch.setattr(settings, "progress_events_enabled", True)
    agent = _agent()
    events = []
    agent.event_sink = events.append
    agent._emit_progress("understanding")
    agent._emit_progress("retrieving", "aftersale")
    agent._emit_progress("generating")
    agent._emit_progress("retrieving", "aftersale")   # 模拟乱序:必须被吞
    stages = [e["stage"] for e in events if e["type"] == "progress"]
    assert stages == ["understanding", "retrieving", "generating"]


def test_new_turn_progress_gate_resets_and_allows_retrieving_again(monkeypatch):
    """跨轮不残留:上一轮在 generating(rank2)结束,下一轮的 retrieving
    (rank1)必须能正常发出,不能被上一轮遗留的阶段位置误判成"倒退"而吞掉
    ——这正是 chat() 每轮开头 reset 单调门要保证的事。"""
    monkeypatch.setattr(settings, "progress_events_enabled", True)
    agent = _agent()
    fake_msg = type("M", (), {"content": "ok", "tool_calls": None})()
    fake_resp = type("R", (), {"choices": [type("C", (), {"message": fake_msg})()]})()
    monkeypatch.setattr(agent, "_llm_create", lambda messages, use_tools: fake_resp)
    agent.memory_manager.update_short_term = lambda *a, **k: None
    agent._reply_pipeline.run = lambda *a, **k: a[3]
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None: RecallResult())
    events = []
    agent.event_sink = events.append
    agent.chat("第一轮")   # 真实 _react_loop 跑完,走到 generating(rank2)结束
    assert agent._turn_progress_gate._last_rank == 2

    events.clear()
    agent.set_turn_understanding(QueryUnderstanding(
        domain="aftersale", intent="政策咨询", need_kb=True, kb_query="第二轮", source="llm"))
    agent.chat("第二轮")   # 新一轮:retrieving(rank1)不该被上一轮的 rank2 挡住
    stages = [e["stage"] for e in events if e["type"] == "progress"]
    assert "retrieving" in stages


def test_stage_message_domain_mapping():
    assert stage_message("understanding") == "正在理解您的问题…"
    assert stage_message("generating") == "正在为您生成回复…"
    assert stage_message("retrieving", "midsale") == "正在为您查询订单和物流信息…"
    assert stage_message("retrieving", "aftersale") == "正在为您查询售后政策…"
    assert stage_message("retrieving", "presale") == "正在为您查询商品与优惠信息…"
    assert stage_message("retrieving", None) == "正在为您检索相关信息…"


# ---- 编排器侧:understanding 阶段 + 并发预取推测阶段 ----

def test_orchestrator_emits_understanding_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "progress_events_enabled", True)
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", False)
    qu = QueryUnderstanding(domain="presale", intent="商品咨询", need_kb=False, source="rule")
    monkeypatch.setattr("app.agent.understanding.understand", lambda *a, **k: qu)
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None: RecallResult())

    o = _orch(tmp_path, monkeypatch)
    events = []
    o.event_sink = events.append
    o.chat("你有什么商品")
    progress = [e for e in events if e["type"] == "progress"]
    assert any(p["stage"] == "understanding" for p in progress)


def test_orchestrator_speculative_retrieving_only_when_concurrent_and_query_long_enough(tmp_path, monkeypatch):
    """原句够长(过 min_query_chars 门槛)时,预取会真的发起一次检索,推测性
    retrieving 提示用通用文案(此刻 domain 还没出来,不冒充具体);且因为
    kb_query 与原句相同,预取被直接复用,_build_messages 里不会再触发第二条
    "确定域"版本的 retrieving。原句过短(如"嗯")时预取压根不会发起,不该有
    这条推测性提示——但 kb_query 强制 True 时 _build_messages 仍会现场检索
    一次,那条"确定域"的 retrieving 是真实、合法的,不是本测试要否定的对象。"""
    monkeypatch.setattr(settings, "progress_events_enabled", True)
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", True)
    monkeypatch.setattr(settings, "recall_kb_enabled", True)
    monkeypatch.setattr(settings, "recall_kb_min_query_chars", 4)
    qu = QueryUnderstanding(domain="aftersale", intent="政策咨询",
                            need_kb=True, kb_query="退货政策是什么", source="llm")
    monkeypatch.setattr("app.agent.understanding.understand", lambda *a, **k: qu)
    monkeypatch.setattr("app.agent.recall.kb.kb_fetch_rows", lambda q: ([], "local"))
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None: RecallResult())

    o = _orch(tmp_path, monkeypatch, "long.json")
    events = []
    o.event_sink = events.append
    o.chat("退货政策是什么")   # 原句与 kb_query 相同,过了 min_query_chars 门槛
    retrieving = [e for e in events if e["type"] == "progress" and e["stage"] == "retrieving"]
    assert len(retrieving) == 1                                    # 预取被复用,没有第二次
    assert retrieving[0]["message"] == "正在为您检索相关信息…"       # domain 未知时用通用文案,不冒充具体

    o2 = _orch(tmp_path, monkeypatch, "short.json")
    events2 = []
    o2.event_sink = events2.append
    o2.chat("嗯")   # 短于门槛,预取不会真的发起——但 qu 仍判 need_kb=True,故
                    # _build_messages 会现场检索一次,那条"确定域"提示是真实的
    retrieving2 = [e for e in events2 if e["type"] == "progress" and e["stage"] == "retrieving"]
    assert len(retrieving2) == 1
    assert retrieving2[0]["message"] == "正在为您查询售后政策…"      # 域已知(aftersale),不是推测性通用文案
