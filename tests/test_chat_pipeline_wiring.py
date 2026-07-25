"""H1.3:EcomAgent.chat() 接入 ReplyPipeline 的分级门控 + 接地上下文测试。

用裸 agent(EcomAgent.__new__ + monkeypatch)风格,参考 tests/test_react_degrade.py,
不触网、不建真 Agent。
"""

import json
import types as _t

from app.agent.chat import EcomAgent
from app.config.settings import settings


# ---- OpenAI 响应对象的最小仿造(与 test_react_degrade.py 一致) ----
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
        self.outer.calls.append("tools" in kwargs)
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
    # R2 checkpoint 所需字段(裸 agent 不走 __init__)
    a.session_path = "x.json"
    a.store = _t.SimpleNamespace(save=lambda k, s: None, load=lambda k: None, delete=lambda k: None)
    a._status = "complete"
    a._step_seq = 0
    a._pending = None
    a.memory_manager = _t.SimpleNamespace(
        stm_to_dict=lambda: {}, update_short_term=lambda msgs, all_messages=None: None,
    )
    from app.agent.reply_pipeline import ReplyPipeline
    a._reply_pipeline = ReplyPipeline()
    return a


def _extract_stub(self, text):
    """跳过 _extract_structured_response 的真 LLM 调用,直接把 text 包成最简结果对象。"""
    return _t.SimpleNamespace(model_dump_json=lambda: json.dumps({"reply": text}), reply=text)


# ---- 1. 复杂轮:调用流水线,且 complex_turn=True、draft 为 react 产出的文本 ----
def test_complex_turn_invokes_pipeline_once_with_draft(monkeypatch):
    monkeypatch.setattr(settings, "reply_pipeline_enabled", True)
    tm = FakeToolManager()
    a = make_agent([
        _Resp(_Msg(content="让我查一下", tool_calls=[_TC("1", "query_order", '{"order_id":"O1"}')])),
        _Resp(_Msg(content="您的订单已发货。", tool_calls=None)),
    ], tm=tm)
    monkeypatch.setattr(EcomAgent, "_extract_structured_response", _extract_stub)

    recorded = {}

    def fake_run(client, model, user_input, draft, grounding, complex_turn, emit):
        recorded["called"] = recorded.get("called", 0) + 1
        recorded["args"] = dict(
            user_input=user_input, draft=draft, grounding=grounding,
            complex_turn=complex_turn,
        )
        return draft

    monkeypatch.setattr(a._reply_pipeline, "run", fake_run)

    a.chat("我的订单到哪了")

    assert recorded["called"] == 1
    assert recorded["args"]["complex_turn"] is True
    assert recorded["args"]["draft"] == "您的订单已发货。"
    assert recorded["args"]["user_input"] == "我的订单到哪了"


# ---- 2. 简单轮:门控 → complex_turn=False(纯问候,不走工具) ----
def test_simple_turn_passes_complex_turn_false(monkeypatch):
    monkeypatch.setattr(settings, "reply_pipeline_enabled", True)
    a = make_agent([
        _Resp(_Msg(content="您好，请问需要什么帮助？", tool_calls=None)),
    ])
    monkeypatch.setattr(EcomAgent, "_extract_structured_response", _extract_stub)

    recorded = {}

    def fake_run(client, model, user_input, draft, grounding, complex_turn, emit):
        recorded["called"] = recorded.get("called", 0) + 1
        recorded["complex_turn"] = complex_turn
        return draft

    monkeypatch.setattr(a._reply_pipeline, "run", fake_run)

    a.chat("你好")

    assert recorded["called"] == 1
    assert recorded["complex_turn"] is False


# ---- 3. _grounding_context() 单测 ----
def test_grounding_context_only_current_turn_tools_truncated_and_ordered():
    a = EcomAgent.__new__(EcomAgent)

    long_content = "Z" * (settings.grounding_result_max_chars + 100)

    a.raw_messages = [
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "0"}]},
        {"role": "tool", "tool_call_id": "0", "content": "OLD"},
        {"role": "assistant", "content": "第一轮回答"},
        {"role": "user", "content": "第二轮问题"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}, {"id": "2"}]},
        {"role": "tool", "tool_call_id": "1", "content": "A"},
        {"role": "tool", "tool_call_id": "2", "content": "B" + long_content},
        {"role": "assistant", "content": "第二轮回答"},
    ]

    ctx = a._grounding_context()

    assert "OLD" not in ctx
    lines = ctx.split("\n")
    assert lines[0] == "A"
    assert lines[1] == ("B" + long_content)[: settings.grounding_result_max_chars]
    assert len(lines[1]) == settings.grounding_result_max_chars


def test_grounding_context_keeps_typical_tool_result_whole():
    """回归:真实缺陷——list_user_orders 结果约 775 字,旧 500 字截断把第 4 单
    拦腰切断、第 5 单切没,评估器把正确草稿判为编造、重写反把回复改坏。
    典型工具结果(<2000 字)必须完整保留。"""
    a = EcomAgent.__new__(EcomAgent)
    orders_like = "X" * 775   # 与 list_user_orders 实测长度同量级

    a.raw_messages = [
        {"role": "user", "content": "查一下我的订单"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": orders_like},
    ]

    ctx = a._grounding_context()
    assert ctx == orders_like          # 完整,一个字不截


def test_grounding_context_total_budget_keeps_most_recent():
    """总预算生效时,优先保住最近的工具结果(逆序累计,超预算即停)。"""
    a = EcomAgent.__new__(EcomAgent)
    per = settings.grounding_result_max_chars
    n = settings.grounding_total_max_chars // per + 2   # 超总预算的条数

    msgs = [{"role": "user", "content": "q"}]
    for i in range(n):
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": f"{i}:" + "Y" * per})
    a.raw_messages = msgs

    ctx = a._grounding_context()
    assert len(ctx) <= settings.grounding_total_max_chars + n   # 换行符余量
    assert f"{n-1}:" in ctx            # 最近的一定在
    assert "0:" not in ctx             # 最早的被总预算挤掉
