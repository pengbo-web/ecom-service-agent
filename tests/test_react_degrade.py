"""Phase 4.1:_react_loop 的两条鲁棒性降级——空回复重试 + 畸形工具调用降级。

用 fake client 脚本化 LLM 响应,直接驱动 _react_loop,不触网、不建真 Agent。
"""

import json

from app.agent.chat import EcomAgent


# ---- OpenAI 响应对象的最小仿造 ----
class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})()]


class _Completions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls.append("tools" in kwargs)   # 记录本次是否带 tools
        return self.outer.script.pop(0)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.chat = type("Chat", (), {"completions": _Completions(self)})()


class FakeToolManager:
    tool_definitions: list = []

    def __init__(self):
        self.executed = []

    def execute_tool(self, name, args):
        self.executed.append((name, args))
        return json.dumps({"success": True, "ok": True})


def make_agent(script, tm=None, max_steps=3):
    a = EcomAgent.__new__(EcomAgent)
    a.client = FakeClient(script)
    a.model = "test"
    a.temperature = 0.0
    a.max_react_steps = max_steps
    a.tool_manager = tm or FakeToolManager()
    a.raw_messages = []
    a.session_id = None
    a.summary = None
    a.events = []
    a.event_sink = lambda ev: a.events.append(ev)
    a._build_messages = lambda: [{"role": "system", "content": "s"}] + a.raw_messages
    return a


def _degrade_reasons(agent):
    return [e.get("reason") for e in agent.events if e.get("type") == "degrade"]


# ---- ① 空回复重试 ----
def test_empty_reply_retries_without_tools_and_recovers():
    a = make_agent([
        _Resp(_Msg(content="", tool_calls=None)),                 # 首次空回复
        _Resp(_Msg(content="您好，请问需要什么帮助？", tool_calls=None)),  # 重试成功
    ])
    out = a._react_loop()
    assert out == "您好，请问需要什么帮助？"
    assert a.client.calls == [True, False]        # 首次带 tools,重试不带
    assert "empty_reply" in _degrade_reasons(a)


def test_empty_reply_retry_still_empty_uses_fallback():
    a = make_agent([
        _Resp(_Msg(content="   ", tool_calls=None)),   # 空白
        _Resp(_Msg(content="", tool_calls=None)),       # 重试仍空
    ])
    out = a._react_loop()
    assert out == EcomAgent._EMPTY_REPLY_FALLBACK
    assert "empty_reply" in _degrade_reasons(a)


# ---- ② 畸形工具调用降级 ----
def test_malformed_tool_args_degrade_to_no_tools_answer():
    tm = FakeToolManager()
    a = make_agent([
        _Resp(_Msg(content=None, tool_calls=[_TC("1", "apply_refund", "{不是合法json")])),
        _Resp(_Msg(content="抱歉，我先帮您核对一下订单信息。", tool_calls=None)),  # 降级后的自然语言回答
    ], tm=tm)
    out = a._react_loop()
    assert out == "抱歉，我先帮您核对一下订单信息。"
    assert a.client.calls == [True, False]           # 降级请求不带 tools
    assert tm.executed == []                          # 畸形调用绝不执行
    assert "malformed_tool_call" in _degrade_reasons(a)
    # 畸形的 assistant(带 tool_calls)不应写进历史,避免留下无结果的孤儿调用
    assert all("tool_calls" not in m for m in a.raw_messages)


def test_missing_tool_name_degrades():
    tm = FakeToolManager()
    a = make_agent([
        _Resp(_Msg(content=None, tool_calls=[_TC("1", "", '{"order_id":"O1"}')])),
        _Resp(_Msg(content="需要您补充一下信息哦。", tool_calls=None)),
    ], tm=tm)
    out = a._react_loop()
    assert out == "需要您补充一下信息哦。"
    assert tm.executed == []
    assert "malformed_tool_call" in _degrade_reasons(a)


# ---- 正常路径不受影响 ----
def test_happy_path_executes_tool_then_answers():
    tm = FakeToolManager()
    a = make_agent([
        _Resp(_Msg(content="让我查一下", tool_calls=[_TC("1", "query_order", '{"order_id":"O1"}')])),
        _Resp(_Msg(content="您的订单已发货。", tool_calls=None)),
    ], tm=tm)
    out = a._react_loop()
    assert out == "您的订单已发货。"
    assert tm.executed == [("query_order", {"order_id": "O1"})]
    assert a.client.calls == [True, True]            # 两步都带 tools,无降级
    assert _degrade_reasons(a) == []
