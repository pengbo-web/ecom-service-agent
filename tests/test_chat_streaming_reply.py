"""E1(回复流式化):EcomAgent._react_loop 第 0 步 stream=True 的行为验证。

用 fake client 脚本化 LLM 的流式/非流式响应,直接驱动 _react_loop,不触网、
不建真 Agent——风格与 tests/test_react_degrade.py / test_chat_pipeline_wiring.py
一致(EcomAgent.__new__ 裸 agent)。

覆盖:
  - 流式且第 0 步就是终答:reply_delta 逐块吐出,拼接结果 == _react_loop 返回值
    (== 最终会写进历史/落盘的文本)；首块带 first=True。
  - 流式但第 0 步模型实际决定调用工具:不发任何 reply_delta,原样降级为与
    非流式等价的 message 对象,工具正常执行,第 1 步(已过 step 0)恒非流式。
  - 总开关关闭 / 本轮未被判定 eligible:第 0 步也走非流式旧路径,不带 stream=True
    ——与"开关关闭行为跟现状逐字节一致"的要求对应。
"""

import json
import types as _t

from app.agent.chat import EcomAgent
from app.config.settings import settings


# ---- OpenAI 非流式响应对象的最小仿造(与 test_react_degrade.py 一致) ----
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


# ---- OpenAI 流式 chunk 的最小仿造:choices[0].delta.{content,tool_calls} ----
class _DeltaToolCall:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments) if (name is not None or arguments is not None) else None


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, delta):
        self.choices = [type("C", (), {"delta": delta})()]


class _StreamResp:
    """脚本里放一个这个,create(stream=True) 命中时把它整个当迭代器返回。"""
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        return iter(self._chunks)


class _Completions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls.append({"tools": "tools" in kwargs, "stream": bool(kwargs.get("stream"))})
        return self.outer.script.pop(0)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list = []
        self.chat = type("Chat", (), {"completions": _Completions(self)})()


class FakeToolManager:
    tool_definitions: list = []

    def __init__(self):
        self.executed = []

    def execute_tool(self, name, args):
        self.executed.append((name, args))
        return json.dumps({"success": True, "ok": True})


def make_agent(script, tm=None, max_steps=3, stream_eligible=True):
    a = EcomAgent.__new__(EcomAgent)
    a.client = FakeClient(script)
    a.model = "test"
    a.temperature = 0.0
    a.max_react_steps = max_steps
    a.tool_manager = tm or FakeToolManager()
    a.raw_messages = []
    a.session_id = None
    a.summary = None
    a.events: list = []
    a.event_sink = lambda ev: a.events.append(ev)
    a._build_messages = lambda: [{"role": "system", "content": "s"}] + a.raw_messages
    a.session_path = "x.json"
    a.store = _t.SimpleNamespace(save=lambda k, s: None, load=lambda k: None, delete=lambda k: None)
    a._status = "complete"
    a._step_seq = 0
    a._pending = None
    a.memory_manager = _t.SimpleNamespace(stm_to_dict=lambda: {})
    a._turn_stream_eligible = stream_eligible
    return a


def _deltas(agent):
    return [e for e in agent.events if e["type"] == "reply_delta"]


def test_stream_first_step_final_answer_concatenates(monkeypatch):
    monkeypatch.setattr(settings, "stream_reply_enabled", True)
    a = make_agent([
        _StreamResp([
            _Chunk(_Delta(content="您的")),
            _Chunk(_Delta(content="订单")),
            _Chunk(_Delta(content="已发货")),
        ]),
    ])
    out = a._react_loop()
    assert out == "您的订单已发货"
    deltas = _deltas(a)
    assert [d["content"] for d in deltas] == ["您的", "订单", "已发货"]
    assert "".join(d["content"] for d in deltas) == out   # 拼接结果 == 最终文本
    assert deltas[0]["first"] is True
    assert all("first" not in d for d in deltas[1:])       # 只有首块带 first
    assert a.client.calls == [{"tools": True, "stream": True}]
    # 落盘断言:_react_loop 直接写进 raw_messages 的就是这份拼接后的完整文本
    # (chat() 里再往下走 reply_pipeline/护栏都是在这份文本上继续处理,不会
    # 绕开它另起一份;这里先确认 react 层这一步本身不走样)。
    assert a.raw_messages[-1] == {"role": "assistant", "content": out}


def test_stream_falls_back_when_first_step_calls_tool(monkeypatch):
    monkeypatch.setattr(settings, "stream_reply_enabled", True)
    tm = FakeToolManager()
    a = make_agent([
        _StreamResp([
            _Chunk(_Delta(tool_calls=[_DeltaToolCall(0, id="1", name="query_order")])),
            _Chunk(_Delta(tool_calls=[_DeltaToolCall(0, arguments='{"order_id":"O1"}')])),
        ]),
        _Resp(_Msg(content="您的订单已发货。", tool_calls=None)),   # 第 1 步:恒非流式
    ], tm=tm)
    out = a._react_loop()
    assert out == "您的订单已发货。"
    assert _deltas(a) == []                     # 工具调用步骤不发任何 reply_delta
    assert tm.executed == [("query_order", {"order_id": "O1"})]
    # 第 0 步 stream=True,第 1 步(已经过 step 0)恒非流式、不带 stream
    assert a.client.calls == [
        {"tools": True, "stream": True},
        {"tools": True, "stream": False},
    ]


def test_stream_switch_off_is_byte_identical_to_non_streaming(monkeypatch):
    """W1 E1 要求:开关关闭 ⇒ 行为与现状逐字节一致——第 0 步也走非流式旧路径。"""
    monkeypatch.setattr(settings, "stream_reply_enabled", False)
    a = make_agent([
        _Resp(_Msg(content="您好，请问需要什么帮助？", tool_calls=None)),
    ], stream_eligible=True)
    out = a._react_loop()
    assert out == "您好，请问需要什么帮助？"
    assert _deltas(a) == []
    assert a.client.calls == [{"tools": True, "stream": False}]


def test_requires_human_keyword_split_across_chunks_is_detected(monkeypatch):
    """上一个任务(E?)的作者特别强调过:requires_human 的关键词扫描必须跑在
    *拼接后*的整段文本上,不能逐块扫——"人工客服"这四个字如果正好被切在两个
    chunk 的边界上,逐块扫会两块都扫不出来,拼完整再扫才能扫到。这里把标记词
    故意切成两半("人工" / "客服"分别落在不同 chunk),验证:
      1) _react_loop 拼接后的整段文本里标记词是完整的;
      2) 用于派生 requires_human 的 _requires_human_from_text 对拼接后的
         整段文本判定为 True(即"会被后续 chat() 正确判定需要转人工")。
    """
    monkeypatch.setattr(settings, "stream_reply_enabled", True)
    a = make_agent([
        _StreamResp([
            _Chunk(_Delta(content="这个问题我解决不了，建议您联系人")),
            _Chunk(_Delta(content="工客服协助处理")),
        ]),
    ])
    out = a._react_loop()
    assert "人工客服" in out                          # 拼接后标记词完整出现
    assert EcomAgent._requires_human_from_text(out) is True
    # 逆向对照:任一单独 chunk 都不含完整标记词(证明"必须拼完再扫"不是摆设)
    deltas = [d["content"] for d in _deltas(a)]
    assert all("人工客服" not in c for c in deltas)


def test_stream_ineligible_turn_is_non_streaming(monkeypatch):
    """本轮未被 streaming.py 判定 eligible(如命中变换类护栏)⇒ 第 0 步也非流式。"""
    monkeypatch.setattr(settings, "stream_reply_enabled", True)
    a = make_agent([
        _Resp(_Msg(content="您的手机号已登记。", tool_calls=None)),
    ], stream_eligible=False)
    out = a._react_loop()
    assert out == "您的手机号已登记。"
    assert _deltas(a) == []
    assert a.client.calls == [{"tools": True, "stream": False}]
