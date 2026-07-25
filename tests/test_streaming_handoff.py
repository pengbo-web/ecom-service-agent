"""转人工闭环 streaming 层:前置短路零LLM/事后升级带QU意图与重复检测。"""

from types import SimpleNamespace

from app.api.streaming import run_agent_streaming
from app.hitl.manager import HitlManager


class FakeAgent:
    """chat 若被调用则记录——前置短路场景中它绝不应被调用。"""
    def __init__(self):
        self.raw_messages = []
        self.user_id = "u1"
        self.chat_called = 0
        self._turn_qu = None
        self._pending = None

    def chat(self, user_input):
        self.chat_called += 1
        return SimpleNamespace(
            reply="正常回答", intent=SimpleNamespace(value="other"),
            confidence=0.9, requires_human=False, follow_up_question=None,
            model_dump_json=lambda: "{}")


def _drain(agent, text, hitl):
    return list(run_agent_streaming(agent, text, session_id="s1", hitl=hitl))


def test_pure_human_request_short_circuits_without_llm(tmp_path, monkeypatch):
    monkeypatch.setattr("app.agent.memory.profile.record_ticket", lambda *a, **k: None)
    hitl = HitlManager(queue=SimpleNamespace(add=lambda bundle: "hid-test-1"),
                       confidence_threshold=0.6)
    agent = FakeAgent()
    events = _drain(agent, "转人工", hitl)
    assert agent.chat_called == 0                     # 零 LLM
    types = [e["type"] for e in events]
    assert "handoff" in types and "reply" in types and "metadata" in types
    handoff = [e for e in events if e["type"] == "handoff"][0]
    assert any("明确要求转人工" in r for r in handoff["reasons"])
    reply = [e for e in events if e["type"] == "reply"][0]
    assert "人工" in reply["content"]


def test_normal_question_not_short_circuited(monkeypatch):
    monkeypatch.setattr("app.agent.memory.profile.record_ticket", lambda *a, **k: None)
    hitl = HitlManager(queue=SimpleNamespace(add=lambda bundle: "hid-test-1"),
                       confidence_threshold=0.6)
    agent = FakeAgent()
    _drain(agent, "人工审核要多久", hitl)
    assert agent.chat_called == 1                     # 正常走 Agent


def test_post_evaluate_receives_qu_intent(monkeypatch):
    monkeypatch.setattr("app.agent.memory.profile.record_ticket", lambda *a, **k: None)
    captured = {}
    hitl = HitlManager(queue=SimpleNamespace(add=lambda bundle: "hid-test-1"),
                       confidence_threshold=0.1)      # 低阈值避免误升
    orig = hitl.evaluate
    def spy(*a, **k):
        captured.update(k)
        return []
    hitl.evaluate = spy
    agent = FakeAgent()
    agent._turn_qu = SimpleNamespace(intent="投诉", need_kb=False,
                                     domain=None, kb_query=None, source="llm")
    agent.raw_messages = [{"role": "user", "content": "上一条"},
                          {"role": "user", "content": "本轮消息"}]
    _drain(agent, "本轮消息", hitl)
    assert captured.get("qu_intent") == "投诉"
    assert captured.get("prior_user_msgs") == ["上一条"]   # 不含本轮
