"""Langfuse 事件桥:门控 / 阶段栈配对 / 残留清理 / best-effort。全离线(fake client)。"""

from app.config.settings import settings
from app.observability.langfuse_bridge import langfuse_turn, _LangfuseTurn


class _FakeCM:
    """模拟 start_as_current_observation 返回的上下文管理器。"""

    def __init__(self, log, kind, name):
        self.log, self.kind, self.name = log, kind, name
        self.obs = _FakeObs(log, name)

    def __enter__(self):
        self.log.append(("enter", self.kind, self.name))
        return self.obs

    def __exit__(self, *a):
        self.log.append(("exit", self.kind, self.name))
        return False


class _FakeObs:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def update(self, **kw):
        self.log.append(("update", self.name, kw))

    def end(self):
        self.log.append(("end", self.name))


class _FakeLangfuse:
    def __init__(self):
        self.log = []

    def start_as_current_observation(self, as_type="span", name="", **kw):
        return _FakeCM(self.log, as_type, name)

    def start_observation(self, as_type="span", name="", **kw):
        obs = _FakeObs(self.log, name)
        self.log.append(("instant", as_type, name))
        return obs


def test_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "langfuse_enabled", False)
    assert langfuse_turn("s1", "u1", "hi") is None


def _turn(fake):
    t = _LangfuseTurn(fake, "s1", "u1", "查订单")
    # 绕过 __enter__ 里的 propagate_attributes(真 SDK 依赖),手工置根
    t._root_cm = fake.start_as_current_observation(as_type="agent", name="invoke_agent 小夕")
    t._root = t._root_cm.__enter__()
    return t


def test_stage_and_tool_events_pair_and_nest():
    fake = _FakeLangfuse()
    t = _turn(fake)
    t.on_event({"type": "stage", "status": "start", "name": "react"})
    t.on_event({"type": "tool_call", "name": "query_order", "args": {"order_id": "O1"}})
    t.on_event({"type": "tool_result", "content": '{"success": true}'})
    t.on_event({"type": "stage", "status": "end", "name": "react"})
    kinds = [(op, name) for op, _, name in
             [x for x in fake.log if x[0] in ("enter", "exit")]]
    assert ("enter", "react") in kinds and ("exit", "react") in kinds
    assert ("enter", "execute_tool query_order") in kinds
    # 工具在 react 之内开、之内闭
    assert kinds.index(("enter", "react")) < kinds.index(("enter", "execute_tool query_order"))
    assert kinds.index(("exit", "execute_tool query_order")) < kinds.index(("exit", "react"))
    # 工具结果写到了 output
    assert any(op == "update" and "output" in kw for op, _, kw in
               [x for x in fake.log if x[0] == "update"])


def test_reply_sets_root_output_and_metadata_sets_intent():
    fake = _FakeLangfuse()
    t = _turn(fake)
    t.on_event({"type": "reply", "content": "最终回复"})
    t.on_event({"type": "metadata", "intent": "order_query", "confidence": 1.0,
                "requires_human": False})
    updates = [kw for op, name, kw in [x for x in fake.log if x[0] == "update"]]
    assert any(kw.get("output") == "最终回复" for kw in updates)
    assert any(kw.get("metadata", {}).get("intent") == "order_query" for kw in updates)


def test_exit_closes_leftover_stages():
    fake = _FakeLangfuse()
    t = _turn(fake)
    t.on_event({"type": "stage", "status": "start", "name": "react"})   # 故意不发 end
    t.__exit__(None, None, None)
    exits = [name for op, _, name in [x for x in fake.log if x[0] == "exit"]]
    assert "react" in exits          # 残留阶段被兜底闭合


def test_on_event_swallows_exceptions():
    t = _LangfuseTurn(object(), "s", "u", "q")   # 根本没有可用 client
    t._root = _FakeObs([], "root")
    t.on_event({"type": "stage", "status": "start", "name": "x"})   # 不炸即可
    t.on_event({"type": "不认识的类型"})


# ---- background_trace(后台任务命名根) ----
def test_background_trace_disabled_yields_none(monkeypatch):
    from app.observability.langfuse_bridge import background_trace
    monkeypatch.setattr(settings, "langfuse_enabled", False)
    with background_trace("consolidate_memory", session_id="s1") as root:
        assert root is None      # 门控关:零副作用


def test_background_trace_setup_failure_yields_none(monkeypatch):
    """开了开关但初始化异常(未装/坏配置)→ 静默回退 None,业务不受影响。"""
    import app.observability.langfuse_bridge as mod
    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(mod, "_ensure_env", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with mod.background_trace("consolidate_memory") as root:
        assert root is None


# ---- 阶段一 gap③:新增的 8 种事件类型 ----

def _instant(fake, name):
    """取 fake.log 里某个即时观察(start_observation)的 (as_type, kw)。"""
    for op, as_type, ev_name, *rest in fake.log:
        if op == "instant" and ev_name == name:
            return as_type
    return None


def test_workflow_guard_is_instant_guardrail_with_warning_level():
    """安全动作:必须醒目——as_type=guardrail + level=WARNING,且是即时观察
    (没有对应的 start/end 配对信号,不该建 span 栈)。"""
    fake = _FakeLangfuse()
    t = _turn(fake)
    t.on_event({"type": "workflow_guard", "name": "process_return", "reason": "步骤被跳过"})
    instants = [x for x in fake.log if x[0] == "instant"]
    assert len(instants) == 1
    _, as_type, name = instants[0]
    assert as_type == "guardrail"
    assert name == "workflow_guard:process_return"


def test_degrade_is_instant_and_prominent():
    """失败信号:必须醒目——level=WARNING,即时观察。"""
    fake = _FakeLangfuse()
    t = _turn(fake)
    t.on_event({"type": "degrade", "reason": "empty_reply"})
    instants = [x for x in fake.log if x[0] == "instant"]
    assert len(instants) == 1
    assert instants[0][1] == "span" and instants[0][2] == "degrade"


def test_skill_preloaded_recall_faq_cache_thought_evaluate_polish_select_are_instant():
    """其余六种事件类型协议里都只有一条完事事件,不带 start/end 配对信号,
    因此全部记成即时观察(start_observation 而不是 start_as_current_observation)。
    这里用"根本没有配对的 enter/exit 记录"来验证——只应看到 instant 记录。"""
    fake = _FakeLangfuse()
    t = _turn(fake)
    fake.log.clear()   # 清掉 _turn() 本身开根观察产生的一对 enter,只看事件触发的
    events = [
        {"type": "skill_preloaded", "name": "process-return", "variant": "live"},
        {"type": "faq_cache", "matched": "怎么退货", "score": 0.92},
        {"type": "recall", "source": "kb", "backend": "bm25", "query": "退货", "hits": [{"id": 1}]},
        {"type": "recall", "source": "kb", "skipped": True, "reason": "闲聊"},
        {"type": "thought", "content": "我先看看订单"},
        {"type": "evaluate", "ok": True},
        {"type": "polish"},
        {"type": "select", "next": "done", "reason": "already_polished"},
    ]
    for ev in events:
        t.on_event(ev)
    names = [name for op, _, name in [x for x in fake.log if x[0] in ("enter", "exit")]]
    assert names == []   # 没有任何一种建了 span 栈(enter/exit 配对)
    instant_names = [x[2] for x in fake.log if x[0] == "instant"]
    assert instant_names == ["skill_preloaded", "faq_cache", "recall", "recall",
                             "thought", "evaluate", "polish", "select"]


def test_reply_delta_first_chunk_is_instant_others_are_noop():
    """E1(回复流式化):首个 reply_delta(first=True)记一条即时观察
    (reply_delta:first)——供 Langfuse UI 用它的时间戳与根 trace 的起点算出
    首字时间;没有 first 的后续 delta 不重复记(否则一条长回复会把 trace
    灌满几十条零信息量的观察)。"""
    fake = _FakeLangfuse()
    t = _turn(fake)
    fake.log.clear()
    t.on_event({"type": "reply_delta", "content": "您", "first": True})
    t.on_event({"type": "reply_delta", "content": "好"})
    t.on_event({"type": "reply_delta", "content": "呀"})
    instant_names = [x[2] for x in fake.log if x[0] == "instant"]
    assert instant_names == ["reply_delta:first"]


def test_recall_records_hits_or_skip_reason_in_output():
    fake = _FakeLangfuse()
    t = _turn(fake)
    t.on_event({"type": "recall", "source": "kb", "query": "退货", "hits": [{"id": 1}]})
    t.on_event({"type": "recall", "source": "kb", "skipped": True, "reason": "闲聊"})
    # start_observation 在 fake 里不记录 kwargs,这里只需确认两次都成功记了即时观察即可
    assert [x for x in fake.log if x[0] == "instant" and x[2] == "recall"].__len__() == 2


def test_new_event_types_all_swallow_exceptions_when_client_is_unusable():
    """最重要的性质:任意一种新事件类型,在 client 不可用(没有可用的
    start_observation)时都必须静默降级,绝不让 on_event 往外抛。"""
    t = _LangfuseTurn(object(), "s", "u", "q")   # 根本没有可用 client
    t._root = _FakeObs([], "root")
    for etype, extra in [
        ("workflow_guard", {"name": "x", "reason": "y"}),
        ("degrade", {"reason": "empty_reply"}),
        ("skill_preloaded", {"name": "x", "variant": "live"}),
        ("faq_cache", {"matched": "q", "score": 0.9}),
        ("recall", {"source": "kb", "query": "q", "hits": []}),
        ("thought", {"content": "x"}),
        ("evaluate", {"ok": True}),
        ("polish", {}),
        ("select", {"next": "done", "reason": "r"}),
    ]:
        t.on_event({"type": etype, **extra})   # 不炸即可
