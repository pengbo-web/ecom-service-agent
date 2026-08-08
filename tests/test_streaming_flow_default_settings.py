"""E1b(Part3):流程级验证——在默认设置下(guardrails_enabled=True 走
build_default_pipeline,跟 app/api/app.py 生产接线一致),一个没有敏感内容、
没有工具调用的正常买家轮次必须真的逐块吐字给买家,不能只是"架构对了但从不
触发"(E1 报告点名的残余风险)。

用裸 EcomAgent(EcomAgent.__new__ + monkeypatch,风格与 tests/test_react_
degrade.py、tests/test_chat_pipeline_wiring.py 一致)+ 脚本化 FakeClient,
经 `app/api/streaming.py::run_agent_streaming` 这条真实编排路径驱动——覆盖
的是"streaming.py 判定 eligible → 注入真 agent → 真 _react_loop/
_can_stream_step → 真 _llm_create_streaming → IncrementalRedactor → SSE
队列"这条完整链路，不是像 test_streaming_reply_delta.py 那样用一个自己模拟
"该不该发 delta"的 FakeAgent。
"""

import types as _t

from app.agent.chat import EcomAgent
from app.agent.reply_pipeline import ReplyPipeline
from app.api.streaming import run_agent_streaming
from app.config.settings import settings
from app.guardrails.pipeline import build_default_pipeline


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, delta):
        self.choices = [type("C", (), {"delta": delta})()]


class _StreamResp:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __iter__(self):
        return iter(self._chunks)


class _Completions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls.append(bool(kwargs.get("stream")))
        return self.outer.script.pop(0)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list = []
        self.chat = type("Chat", (), {"completions": _Completions(self)})()


class FakeToolManager:
    tool_definitions: list = []


def _extract_stub(self, text):
    """跳过 _extract_structured_response 的真实字段派生逻辑,只关心
    reply/requires_human——这条测试的关注点是"流没流",不是元数据组装。"""
    return _t.SimpleNamespace(
        model_dump_json=lambda: "{}", reply=text,
        intent=_t.SimpleNamespace(value="other"), confidence=0.5,
        requires_human=False, follow_up_question=None,
    )


def make_agent(script, session_path):
    a = EcomAgent.__new__(EcomAgent)
    a.client = FakeClient(script)
    a.model = "test"
    a.temperature = 0.0
    a.max_react_steps = 3
    a.tool_manager = FakeToolManager()
    a.raw_messages = []
    a.session_id = None
    a.user_id = None
    a.summary = None
    a.event_sink = None
    a.session_path = session_path
    a.store = _t.SimpleNamespace(save=lambda k, s: None, load=lambda k: None, delete=lambda k: None)
    a._status = "complete"
    a._step_seq = 0
    a._pending = None
    a._turn_qu = None
    a._turn_recall = None
    a._turn_item_ctx = None
    a._turn_skill_ctx = None
    a._turn_stream_eligible = False
    a.skill_manager = _t.SimpleNamespace(enabled=False)
    a.memory_manager = _t.SimpleNamespace(
        stm_to_dict=lambda: {}, update_short_term=lambda msgs, all_messages=None: None,
    )
    a._reply_pipeline = ReplyPipeline()
    a._build_messages = lambda: [{"role": "system", "content": "s"}] + a.raw_messages
    return a


def test_clean_no_tool_turn_streams_under_default_guardrail_settings(monkeypatch, tmp_path):
    """默认设置(不改任何开关):guardrails_enabled=True(app.py 生产接线用
    build_default_pipeline)、stream_reply_enabled=True、reply_pipeline_
    enabled 对本场景(第 0 步、无工具调用)无影响。一个够长、干净的回复必须
    真的产生 reply_delta 事件——这是"架构对了但从不触发"这条残余风险的
    直接反证。"""
    monkeypatch.setattr(settings, "stream_reply_enabled", True)
    monkeypatch.setattr(settings, "guardrails_enabled", True)
    monkeypatch.setattr(settings, "session_snapshot_enabled", False)
    monkeypatch.setattr(settings, "emotion_trace_enabled", False)
    monkeypatch.setattr(settings, "skill_trace_enabled", False)
    monkeypatch.setattr(settings, "memory_enabled", False)
    monkeypatch.setattr(settings, "skill_preload_enabled", False)
    monkeypatch.setattr(settings, "faq_cache_enabled", False)

    long_reply = ("您的订单已经发货，预计三到五天送达，期间物流信息会实时更新，"
                  "如果有任何问题欢迎随时联系我们客服团队，我们会一直跟进到您收货为止。") * 2
    assert len(long_reply) > build_default_pipeline().local_redaction_holdback()

    chunks = [_Chunk(_Delta(content=ch)) for ch in long_reply]
    agent = make_agent([_StreamResp(chunks)], str(tmp_path / "s.json"))
    monkeypatch.setattr(EcomAgent, "_extract_structured_response", _extract_stub)

    guard_pipeline = build_default_pipeline()   # 与 app/api/app.py 生产接线一致
    events = list(run_agent_streaming(agent, "我的订单发货了吗", guard_pipeline=guard_pipeline))

    deltas = [e for e in events if e["type"] == "reply_delta"]
    assert len(deltas) > 0, "默认设置下一个正常轮次必须真的产生增量帧,不能只是架构对了但从不触发"
    reply_event = [e for e in events if e["type"] == "reply"][0]
    assert reply_event["content"] == long_reply   # 终帧完整、权威
    # 增量帧拼接起来是终帧的一个前缀(尾部 holdback 区间按设计被丢弃,由终帧兜底)
    assert long_reply.startswith("".join(d["content"] for d in deltas))
    assert agent.client.calls[0] is True   # 第 0 步真的以 stream=True 发起
