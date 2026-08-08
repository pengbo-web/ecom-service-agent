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


def test_rewriting_guard_active_still_streams_with_incremental_redaction(monkeypatch):
    """E1b:默认 pipeline 的 SensitiveInfoGuard/ContactInfoGuard 都证明是
    "局部脱敏"(暴露 PATTERNS 且宽度可静态算出,见
    `GuardPipeline.local_redaction_holdback`)——不再像 E1 报告里那样直接
    判 eligible=False、整条不流,而是 eligible=True + IncrementalRedactor
    顶一层缓冲:reply_delta 照常逐块流出(这正是本任务要解决的"46 秒空白屏"
    问题),敏感号码在流式分片里也已经脱敏,终帧同样干净。"""
    monkeypatch.setattr(settings, "stream_reply_enabled", True)
    p = build_default_pipeline()
    assert p.has_rewriting_output_guard() is True   # 前提:默认 pipeline 确实是变换类
    assert p.local_redaction_holdback() is not None  # 前提:且都证明是局部脱敏
    agent = StreamAwareFakeAgent(["您的手机号 ", "13812345678", " 已登记"])
    events = list(run_agent_streaming(agent, "查一下", guard_pipeline=p))
    types = [e["type"] for e in events]
    assert agent.eligible is True   # E1b:不再因为存在变换类护栏就整体禁流
    reply = [e for e in events if e["type"] == "reply"][0]
    assert "13812345678" not in reply["content"]
    # 无论 reply_delta 是否因为 holdback 缓冲而暂未提交,已经发出去的每一个
    # 分片里都不能出现完整的原始手机号——这是"不泄漏"这条硬约束落到流式
    # 分片层面的验证,不依赖它有没有真的发出增量帧。
    deltas = [e for e in events if e["type"] == "reply_delta"]
    for d in deltas:
        assert "13812345678" not in d["content"]


def test_long_clean_reply_streams_incrementally_under_default_guards(monkeypatch):
    """E1b(Part3):正常买家轮次(无敏感内容、够长)在默认护栏配置下必须真的
    逐块吐字,不能只是"架构对了但从不触发"——这正是 E1 报告点名的残余风险,
    这里补一条会失败的旧行为回归测试。"""
    monkeypatch.setattr(settings, "stream_reply_enabled", True)
    p = build_default_pipeline()
    holdback = p.local_redaction_holdback()
    assert holdback is not None
    # 造一段明显长过 holdback 的干净文本,拆成很多小 chunk(模拟真实 token 流)
    long_text = "您的订单已经发货，预计三到五天送达，如有问题欢迎随时联系我们客服团队为您跟进物流详情。" * 3
    assert len(long_text) > holdback
    chunks = [long_text[i:i + 2] for i in range(0, len(long_text), 2)]
    agent = StreamAwareFakeAgent(chunks, reply=long_text)
    events = list(run_agent_streaming(agent, "我的订单发货了吗", guard_pipeline=p))
    deltas = [e for e in events if e["type"] == "reply_delta"]
    assert len(deltas) > 1   # 真的产生了不止一条增量帧——不是"从不触发"
    assert "".join(d["content"] for d in deltas) != ""
    reply = [e for e in events if e["type"] == "reply"][0]
    assert reply["content"] == long_text


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
