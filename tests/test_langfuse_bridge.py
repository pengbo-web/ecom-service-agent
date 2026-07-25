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
