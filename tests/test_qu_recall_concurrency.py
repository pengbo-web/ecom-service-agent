"""L3①:查询理解(QU)与知识召回(KB)并发发起。

核心断言:
  - 并发路径与串行路径对**同一轮**必须产出相同的 QueryUnderstanding 与
    相同的最终检索内容(约束:"同一轮必须产出相同的理解结果与相同的检索
    内容"的直接证明)。
  - 三种"预取不可复用"场景(未开并发/need_kb=False/kb_query 改写导致不
    一致)都正确回退到现场检索,不冒充结果。
  - 总开关 qu_recall_concurrent_enabled=False 完全回退老串行路径。
"""

import threading
import time

from app.agent.recall.service import RecallResult
from app.agent.recall.kb import KbRecall
from app.agent.understanding import QueryUnderstanding
from app.config.settings import settings
from app.multi_agent.orchestrator import MultiAgentOrchestrator


def _orch(tmp_path, monkeypatch, name="s.json"):
    o = MultiAgentOrchestrator(session_path=str(tmp_path / name), user_id="u1")
    o.engine._react_loop = lambda: '{"intent":"other","confidence":0.9,"reply":"ok","requires_human":false}'
    o.engine.memory_manager.update_short_term = lambda *a, **k: None
    o.engine._reply_pipeline.run = lambda *a, **k: a[3]
    return o


def _fixed_understand(qu: QueryUnderstanding):
    # `**_kw` 吸收 understand() 后加的关键字参数(如 outreach_hint):这些桩关心
    # 的是「understand 被调了、返回什么」,不是它的完整签名。写死位置参数的后果
    # 是真实函数每加一个可选参数,这一族测试就集体 TypeError。
    return lambda user_input, history, client, model, **_kw: qu


def _fixed_kb_fetch(rows, backend="local"):
    """替身:kb_fetch_rows(query) 的桩,忽略 query 参数,固定返回同一批行——
    用来验证"预取用的查询"与"现场查询"在同一次真实检索里应该拿到同样的行。"""
    return lambda query: (rows, backend)


class TestConcurrentPathMatchesSerial:
    """同一轮:并发路径与串行路径必须产出相同的理解结果 + 相同的检索内容。"""

    def _run(self, tmp_path, monkeypatch, concurrent: bool, name: str):
        monkeypatch.setattr(settings, "query_understanding_enabled", True)
        monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", concurrent)
        qu = QueryUnderstanding(domain="aftersale", intent="政策咨询",
                                need_kb=True, kb_query="退货政策是什么", source="llm")
        monkeypatch.setattr("app.agent.understanding.understand",
                            _fixed_understand(qu))
        rows = [{"doc": "退换货政策", "section": "七天无理由",
                 "score": 0.8, "text": "签收7天内可退"}]
        monkeypatch.setattr("app.agent.recall.kb.kb_fetch_rows", _fixed_kb_fetch(rows))
        import app.agent.recall.service as svc

        def fake_kb_recall(query, domain=None):
            assert query == "退货政策是什么"   # 现场检索必须用 QU 改写后的 kb_query
            from app.agent.recall.kb import kb_format
            return kb_format(rows, "local", domain)
        monkeypatch.setattr(svc, "kb_recall", fake_kb_recall)

        o = _orch(tmp_path, monkeypatch, name)
        o.chat("退货政策是什么")   # 原句与 kb_query 相同 → 预取应该被直接复用(并发路径下)
        return o

    def test_qu_result_identical(self, tmp_path, monkeypatch):
        o_serial = self._run(tmp_path, monkeypatch, concurrent=False, name="serial.json")
        o_conc = self._run(tmp_path, monkeypatch, concurrent=True, name="conc.json")
        assert o_serial.engine._turn_qu == o_conc.engine._turn_qu

    def test_recall_content_identical(self, tmp_path, monkeypatch):
        o_serial = self._run(tmp_path, monkeypatch, concurrent=False, name="serial2.json")
        o_conc = self._run(tmp_path, monkeypatch, concurrent=True, name="conc2.json")
        rr_serial: RecallResult = o_serial.engine._turn_recall[1]
        rr_conc: RecallResult = o_conc.engine._turn_recall[1]
        assert rr_serial.sections == rr_conc.sections
        assert rr_serial.kb_hits == rr_conc.kb_hits
        assert rr_serial.kb_backend == rr_conc.kb_backend


def test_prefetch_set_on_engine_when_fetch_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", True)
    qu = QueryUnderstanding(domain="presale", intent="商品咨询", need_kb=False, source="rule")
    monkeypatch.setattr("app.agent.understanding.understand", _fixed_understand(qu))
    rows = [{"doc": "d", "section": "s", "score": 0.9, "text": "t"}]
    monkeypatch.setattr("app.agent.recall.kb.kb_fetch_rows", _fixed_kb_fetch(rows))
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None: RecallResult())

    o = _orch(tmp_path, monkeypatch)
    o.chat("你有什么商品")
    query, future = o.engine._turn_kb_prefetch
    assert query == "你有什么商品"
    assert future.result(timeout=2) == (rows, "local")


def test_prefetch_cleared_when_fetch_returns_none(tmp_path, monkeypatch):
    """kb_fetch_rows 判定"这轮压根不会检索"(总开关关/原句过短)时返回 None,
    orchestrator 必须显式清空引擎上的预取字段,不留旧值。"""
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", True)
    qu = QueryUnderstanding(domain=None, intent="闲聊寒暄", need_kb=False, source="rule")
    monkeypatch.setattr("app.agent.understanding.understand", _fixed_understand(qu))
    monkeypatch.setattr("app.agent.recall.kb.kb_fetch_rows", lambda query: None)
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None: RecallResult())

    o = _orch(tmp_path, monkeypatch)
    o.engine._turn_kb_prefetch = ("上一轮残留", ["旧行"], "local")   # 模拟跨轮残留
    o.chat("嗯")
    assert o.engine._turn_kb_prefetch is None   # 必须被清空,不能让旧值漏到本轮


def test_switch_off_falls_back_to_fully_serial(tmp_path, monkeypatch):
    """关 qu_recall_concurrent_enabled:直接调用 understanding.understand(不经
    _understand_and_prefetch),引擎上不设任何预取。"""
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", False)
    qu = QueryUnderstanding(domain="aftersale", intent="政策咨询",
                            need_kb=True, kb_query="退货政策是什么", source="llm")
    calls = []

    def fake_understand(user_input, history, client, model, **_kw):
        calls.append(user_input)
        return qu
    monkeypatch.setattr("app.agent.understanding.understand", fake_understand)

    def boom(*a, **k):
        raise AssertionError("关开关后不该发起并发预取")
    monkeypatch.setattr("app.agent.recall.kb.kb_fetch_rows", boom)
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None, kb_prefetch=None: RecallResult())

    o = _orch(tmp_path, monkeypatch)
    o.chat("退货政策是什么")
    assert calls == ["退货政策是什么"]
    assert o.engine._turn_kb_prefetch is None
    assert o.engine._turn_qu == qu


def test_prefetch_does_not_block_understand_call(tmp_path, monkeypatch):
    """关键正确性:提交 KB 预取后必须**立即**接着跑 understand(),不能等 KB
    检索先跑完——这是第一版实现踩过的坑(两个线程都 join 完才返回,一旦 KB
    检索比 QU 规则快筛慢很多,"并发"反而变成"谁慢等谁",已用真实 ApeRAG
    实测证实过这个反例)。改成 Future 惰性消费后:understand() 在**当前
    线程**同步执行,根本不需要等后台的 KB 检索线程——用一个卡在
    threading.Event 上的 kb_fetch_rows 桩验证:即使 KB 检索线程被人为无限期
    卡住,`route` 事件(信号:understand() 已经跑完)也能立刻发出来,不必等
    KB 检索释放。"""
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", True)

    kb_release = threading.Event()

    def stuck_kb_fetch(query):
        kb_release.wait(timeout=5)   # 模拟一次异常慢的 KB 检索(测试结束前释放)
        return ([{"doc": "d", "section": "s", "score": 0.9, "text": "t"}], "local")

    monkeypatch.setattr("app.agent.recall.kb.kb_fetch_rows", stuck_kb_fetch)
    monkeypatch.setattr("app.agent.understanding.understand",
                        lambda user_input, history, client, model, **_kw: QueryUnderstanding(
                            domain="aftersale", intent="政策咨询",
                            need_kb=True, kb_query=user_input, source="llm"))
    # engine 侧也不能被"等 KB 检索"卡住到发不出 route 事件——react_loop 桩掉,
    # 只让它走到 route 事件发出的那一步,不触发 _build_messages(避免这条测试
    # 本身又卡在 .result() 上,那是另一条测试——test_kb_query_rewrite_* 等——
    # 要覆盖的行为)。
    o = _orch(tmp_path, monkeypatch)
    events = []
    o.event_sink = events.append

    done = threading.Event()

    def _drive():
        o.chat("退货政策是什么")
        done.set()

    t0 = time.time()
    threading.Thread(target=_drive, daemon=True).start()
    # route 事件由 understand() 结果驱动,必须能在 KB 检索被释放之前就发出来——
    # 如果 route 事件迟迟不来,说明 understand() 被 KB 检索的线程卡住了(回归)。
    for _ in range(200):
        if any(e.get("type") == "route" for e in events):
            break
        time.sleep(0.01)
    route_elapsed = time.time() - t0
    kb_release.set()   # 放开卡住的 KB 检索,让 chat() 能收尾,避免测试遗留悬挂线程
    assert done.wait(timeout=5), "释放 KB 检索后 chat() 应该能收尾"
    assert any(e.get("type") == "route" for e in events), "route 事件应该已经发出"
    assert route_elapsed < 1.0, "route 事件不该等 KB 检索释放才发出(说明被卡住了)"


def test_kb_query_rewrite_no_longer_triggers_a_second_retrieval(tmp_path, monkeypatch):
    """R1 契约变更(与改造前相反,故意的):kb_query 与预取 query 不同时,
    **不再**现场补一次阻塞检索——预取的行被直接复用,并发一条 kb_prefetch_reused
    事件如实记录"注入的知识是按哪个 query 检出来的"。

    改造前这里断言的是"必须现场重新检索一次";那个条件在生产里几乎每轮都
    成立(QU 几乎总会改写措辞),等于每轮两次阻塞检索、首字多等一整个 ApeRAG
    往返(实测 11.8s)。取舍论证见 .superpowers/sdd/r1-report.md。"""
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", True)
    qu = QueryUnderstanding(domain="aftersale", intent="政策咨询",
                            need_kb=True, kb_query="退货运费谁承担", source="llm")
    monkeypatch.setattr("app.agent.understanding.understand", _fixed_understand(qu))

    raw_rows = [{"doc": "原句预取结果", "section": "x", "score": 0.9, "text": "按原句检出来的"}]
    monkeypatch.setattr("app.agent.recall.kb.kb_fetch_rows", _fixed_kb_fetch(raw_rows))

    import app.agent.recall.service as svc
    fresh_calls = []

    def fake_kb_recall(query, domain=None):
        fresh_calls.append(query)
        return KbRecall(section="【平台知识(自动检索)】第二次检索的结果",
                        hits=[{"doc": "d", "section": "s", "score": 1.0}])
    monkeypatch.setattr(svc, "kb_recall", fake_kb_recall)

    o = _orch(tmp_path, monkeypatch)
    events = []
    o.event_sink = events.append
    o.chat("那运费呢?")   # 原句与 kb_query("退货运费谁承担")不同

    assert fresh_calls == []   # 关键:没有第二次检索
    rr = o.engine._turn_recall[1]
    assert "按原句检出来的" in rr.sections[0]["content"]
    reused = [e for e in events if e.get("type") == "kb_prefetch_reused"]
    assert len(reused) == 1
    assert reused[0]["final_query"] == "退货运费谁承担"
    assert reused[0]["prefetch_query"] != "退货运费谁承担"   # 复用的确实是另一个 query 的结果


# ---- R1:用打桩的检索客户端数"这一轮到底对检索服务发了几次调用" ----
#
# 打桩打在 app.agent.recall.external_kb.aperag_search 上(kb_backend="aperag"),
# 也就是**真正发 HTTP 的那一层**——不是打在更上面的 kb_recall/kb_fetch_rows 上。
# 这样数出来的就是验收口径里那个"对 ApeRAG 的调用次数",预取那条链路和现场
# 检索那条链路都必然经过它,谁也绕不过去。

def _count_aperag(monkeypatch, rows=None, boom=False):
    """把 aperag_search 换成计数桩,返回记录调用 query 的 list。"""
    calls: list[str] = []
    payload = rows if rows is not None else [
        {"doc": "退换货政策", "section": "运费", "score": 0.9, "text": "签收7天内可退"}]

    def _stub(query):
        calls.append(query)
        if boom:
            raise RuntimeError("aperag down")
        return payload

    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search", _stub)
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr(settings, "kb_local_fallback_enabled", False)
    monkeypatch.setattr(settings, "recall_kb_enabled", True)
    monkeypatch.setattr(settings, "recall_kb_min_query_chars", 4)
    monkeypatch.setattr(settings, "query_understanding_enabled", True)
    return calls


_LONG_INPUT = "我买的洗衣机想退货运费要我出吗"


def test_exactly_one_retrieval_call_per_turn_on_concurrent_path(tmp_path, monkeypatch):
    """验收条款①:同一轮请求里对 ApeRAG 的调用次数 = 1(改造前是 2)。
    场景取的正是"QU 改写了 query"这一最常见的情况——改造前这里必然是 2 次。"""
    calls = _count_aperag(monkeypatch)
    monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", True)
    qu = QueryUnderstanding(domain="aftersale", intent="政策咨询",
                            need_kb=True, kb_query="退货运费谁承担", source="llm")
    monkeypatch.setattr("app.agent.understanding.understand", _fixed_understand(qu))

    o = _orch(tmp_path, monkeypatch, "one_call.json")
    o.chat(_LONG_INPUT)

    assert calls == [_LONG_INPUT], f"本轮检索调用应恰好 1 次(预取那次),实际:{calls}"
    rr = o.engine._turn_recall[1]
    assert rr.kb_hits, "复用的预取结果必须真的被注入,而不是既省了检索也省掉了知识"


def test_need_kb_false_injects_nothing_and_leaves_a_trace(tmp_path, monkeypatch):
    """门控语义不能被这次优化破坏:need_kb=False 时预取结果**绝不注入**,
    且丢弃必须留痕(fail-soft 留痕约束)。"""
    calls = _count_aperag(monkeypatch)
    monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", True)
    qu = QueryUnderstanding(domain="presale", intent="闲聊寒暄", need_kb=False, source="llm")
    monkeypatch.setattr("app.agent.understanding.understand", _fixed_understand(qu))

    o = _orch(tmp_path, monkeypatch, "no_kb.json")
    events = []
    o.event_sink = events.append
    o.chat(_LONG_INPUT)

    rr = o.engine._turn_recall[1]
    assert rr.kb_hits == []
    assert rr.kb_backend == "skipped"
    assert not any("平台知识" in s.get("content", "") for s in rr.sections), \
        "门控判定本轮不需要知识,预取的行一个字都不能进 prompt"
    discarded = [e for e in events if e.get("type") == "kb_prefetch_discarded"]
    assert len(discarded) == 1
    assert discarded[0]["reason"] == "need_kb_false"
    assert discarded[0]["intent"] == "闲聊寒暄"
    # 预取那次调用仍然发生过(它提交在 QU 出结果之前,这是并发的代价,不是缺陷)
    assert len(calls) == 1


def test_switch_off_falls_back_to_serial_single_retrieval(tmp_path, monkeypatch):
    """总开关关掉:回到串行——没有预取,现场按 QU 改写后的 kb_query 检索一次,
    仍然只有 1 次调用,且用的是改写后的 query(串行路径的召回质量不受影响)。"""
    calls = _count_aperag(monkeypatch)
    monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", False)
    qu = QueryUnderstanding(domain="aftersale", intent="政策咨询",
                            need_kb=True, kb_query="退货运费谁承担", source="llm")
    monkeypatch.setattr("app.agent.understanding.understand", _fixed_understand(qu))

    o = _orch(tmp_path, monkeypatch, "serial_one.json")
    o.chat(_LONG_INPUT)

    assert calls == ["退货运费谁承担"], f"串行路径应只检索一次且用改写后的 query,实际:{calls}"
    assert o.engine._turn_kb_prefetch is None
    assert o.engine._turn_recall[1].kb_hits


def test_prefetch_failure_yields_no_injection_and_no_second_retrieval(tmp_path, monkeypatch):
    """预取失败 → 这一轮没有知识注入(全局约束:检索失败保持非致命),
    但**不能**因此现场补一次检索(那就是第二次阻塞检索);失败必须留痕。"""
    calls = _count_aperag(monkeypatch, boom=True)
    monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", True)
    qu = QueryUnderstanding(domain="aftersale", intent="政策咨询",
                            need_kb=True, kb_query="退货运费谁承担", source="llm")
    monkeypatch.setattr("app.agent.understanding.understand", _fixed_understand(qu))

    o = _orch(tmp_path, monkeypatch, "pf_fail.json")
    events = []
    o.event_sink = events.append
    result = o.chat(_LONG_INPUT)

    assert len(calls) == 1, f"预取失败后不该再补一次检索,实际调用:{calls}"
    rr = o.engine._turn_recall[1]
    assert rr.kb_hits == []
    assert rr.kb_backend == "unavailable"   # 与门控主动跳过的 "skipped" 区分开
    discarded = [e for e in events if e.get("type") == "kb_prefetch_discarded"]
    assert len(discarded) == 1
    assert discarded[0]["reason"] == "prefetch_failed"
    assert result is not None   # 买家仍然拿到回复,只是这一轮没有知识注入


# ---- R1:预取 query 的零 LLM 上下文补全 ----

class TestPrefetchQuery:
    """`_prefetch_query`:预取发生在 QU 之前,拿不到改写后的 kb_query,只能用
    原句;对指代/省略型短句补上最近一条买家话(纯字符串拼接,零 LLM)。"""

    def _o(self, tmp_path, monkeypatch, history=()):
        o = _orch(tmp_path, monkeypatch, "pq.json")
        o.engine.raw_messages = list(history)
        return o

    def test_short_followup_gets_previous_user_turn_prepended(self, tmp_path, monkeypatch):
        o = self._o(tmp_path, monkeypatch,
                    [{"role": "user", "content": "我想退货"},
                     {"role": "assistant", "content": "好的"}])
        assert o._prefetch_query("那运费呢?") == "我想退货 那运费呢?"

    def test_self_contained_long_sentence_is_left_alone(self, tmp_path, monkeypatch):
        """自包含长句拼上不相关的上一轮话题只会污染向量查询,是净损失。"""
        o = self._o(tmp_path, monkeypatch,
                    [{"role": "user", "content": "我想买个吹风机有推荐吗"}])
        q = "七天无理由退货是从签收当天开始算吗"
        assert o._prefetch_query(q) == q

    def test_first_turn_has_no_context_to_borrow(self, tmp_path, monkeypatch):
        o = self._o(tmp_path, monkeypatch)
        assert o._prefetch_query("那运费呢?") == "那运费呢?"

    def test_switch_off_restores_raw_sentence_behaviour(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "qu_recall_prefetch_context_enabled", False)
        o = self._o(tmp_path, monkeypatch, [{"role": "user", "content": "我想退货"}])
        assert o._prefetch_query("那运费呢?") == "那运费呢?"

    def test_prefetch_actually_retrieves_with_the_enriched_query(self, tmp_path, monkeypatch):
        """端到端:补全后的 query 真的被送进了检索调用(不是算完就丢)。"""
        calls = _count_aperag(monkeypatch)
        monkeypatch.setattr(settings, "qu_recall_concurrent_enabled", True)
        qu = QueryUnderstanding(domain="aftersale", intent="政策咨询",
                                need_kb=True, kb_query="退货运费谁承担", source="llm")
        monkeypatch.setattr("app.agent.understanding.understand", _fixed_understand(qu))
        o = _orch(tmp_path, monkeypatch, "enriched.json")
        o.engine.raw_messages = [{"role": "user", "content": "我想退货"}]
        o.chat("那运费呢?")
        assert calls == ["我想退货 那运费呢?"]
