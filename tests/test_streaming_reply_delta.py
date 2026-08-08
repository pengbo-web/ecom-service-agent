"""E1(回复流式化):app/api/streaming.py 对 reply_delta 的编排验证。

FakeAgent 模拟"真实 EcomAgent 会做的事"——收到 set_turn_stream_eligible(True)
才在 chat() 内通过 event_sink 发 reply_delta(与 chat.py `_can_stream_first_step`
的真实逻辑同构),从而在不建真 Agent/不触网的前提下验证 streaming.py 那一段
"guard_pipeline 是否含变换类输出护栏 → 决定 eligible → 注入给 agent"的编排
是否正确——这正是本任务的核心决策点。
"""

from app.api.streaming import run_agent_streaming
from app.config.settings import settings
from app.guardrails.pipeline import build_default_pipeline
from app.schemas.response import CustomerServiceResponse, IntentType


class StreamAwareFakeAgent:
    """chat() 内部行为与 EcomAgent._react_loop 的流式分支同构:只有被上层
    判定 eligible 时才发 reply_delta,否则直接一次性产出 reply。"""

    def __init__(self, chunks, reply=None):
        self.event_sink = None
        self.client = None
        self._chunks = chunks
        self._reply = reply if reply is not None else "".join(chunks)
        self.eligible = None
        self.chat_called = False

    def set_turn_stream_eligible(self, eligible: bool) -> None:
        self.eligible = eligible

    def chat(self, user_input):
        self.chat_called = True
        if self.eligible:
            for i, c in enumerate(self._chunks):
                ev = {"type": "reply_delta", "content": c}
                if i == 0:
                    ev["first"] = True
                self.event_sink(ev)
        return CustomerServiceResponse(
            intent=IntentType.GREETING, confidence=0.9,
            reply=self._reply, requires_human=False, follow_up_question=None,
        )


def test_streams_when_no_rewriting_guard_active(monkeypatch):
    """无 guard_pipeline(或 pipeline 里没有变换类输出护栏)⇒ eligible=True,
    reply_delta 逐块流出,拼接结果 == 终帧(reply)全文。"""
    monkeypatch.setattr(settings, "stream_reply_enabled", True)
    agent = StreamAwareFakeAgent(["您好，", "有什么", "可以帮您"])
    events = list(run_agent_streaming(agent, "你好"))
    types = [e["type"] for e in events]
    assert "reply_delta" in types
    deltas = [e for e in events if e["type"] == "reply_delta"]
    assert "".join(d["content"] for d in deltas) == [e for e in events if e["type"] == "reply"][0]["content"]
    assert agent.eligible is True


def test_rewriting_guard_active_emits_zero_incremental_frames(monkeypatch):
    """命中变换类护栏(默认 pipeline 的 SensitiveInfoGuard/ContactInfoGuard)
    ⇒ eligible=False,agent 完全不发 reply_delta——即"这一轮命中改写类护栏时
    不发 reply_delta"这条硬约束,在 streaming.py 编排层面就已经堵死,不依赖
    生成出来的具体文本是否真的触发正则匹配(见 app/guardrails/pipeline.py
    `has_rewriting_output_guard` 的分类式判断)。"""
    monkeypatch.setattr(settings, "stream_reply_enabled", True)
    p = build_default_pipeline()
    assert p.has_rewriting_output_guard() is True   # 前提:默认 pipeline 确实是变换类
    agent = StreamAwareFakeAgent(["您的手机号 ", "13812345678", " 已登记"])
    events = list(run_agent_streaming(agent, "查一下", guard_pipeline=p))
    types = [e["type"] for e in events]
    assert "reply_delta" not in types
    assert agent.eligible is False
    # 全量生成后护栏跑完、脱敏生效,一次性发出终帧——跟现状行为一致
    reply = [e for e in events if e["type"] == "reply"][0]
    assert "13812345678" not in reply["content"]


def test_switch_off_matches_current_frame_sequence(monkeypatch):
    """总开关关闭 ⇒ 即便没有任何护栏,也不发 reply_delta——帧序列与现状
    (tests/test_streaming_guard.py::test_no_pipeline_unchanged)逐字节一致。"""
    monkeypatch.setattr(settings, "stream_reply_enabled", False)
    agent = StreamAwareFakeAgent(["你好"])
    events = list(run_agent_streaming(agent, "你好"))
    assert [e["type"] for e in events] == ["reply", "metadata", "done"]
    assert agent.eligible is False


def test_eligibility_setter_is_optional_for_legacy_fake_agents():
    """没有 set_turn_stream_eligible 方法的桩 agent(如既有测试用的
    FakeAgent)不应报错——防御式 getattr,老测试零改动继续通过。"""
    class LegacyFakeAgent:
        def __init__(self):
            self.event_sink = None
            self.client = None

        def chat(self, user_input):
            return CustomerServiceResponse(
                intent=IntentType.GREETING, confidence=0.9,
                reply="你好", requires_human=False, follow_up_question=None,
            )

    events = list(run_agent_streaming(LegacyFakeAgent(), "你好"))
    assert [e["type"] for e in events] == ["reply", "metadata", "done"]
