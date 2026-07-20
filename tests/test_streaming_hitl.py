import itertools

from app.api.streaming import run_agent_streaming
from app.hitl.manager import HitlManager
from app.hitl.manual_mode import ManualMode
from app.hitl.queue import HandoffQueue
from app.schemas.response import CustomerServiceResponse, IntentType


class FakeAgent:
    def __init__(self, intent=IntentType.COMPLAINT, confidence=0.4, requires_human=True):
        self.event_sink = None
        self.client = None
        self.raw_messages = [{"role": "user", "content": "你们太差了"}]
        self._r = CustomerServiceResponse(
            intent=intent, confidence=confidence, reply="非常抱歉给您带来不便",
            requires_human=requires_human, follow_up_question=None,
        )

    def chat(self, user_input):
        return self._r


def _hitl(tmp_path):
    ids = itertools.count(1)
    q = HandoffQueue(str(tmp_path / "h.db"), id_factory=lambda: f"h{next(ids)}")
    q.init_schema()
    return HitlManager(q, ManualMode(3600), confidence_threshold=0.6), q


def test_escalation_emits_handoff_and_enqueues(tmp_path):
    hitl, q = _hitl(tmp_path)
    events = list(run_agent_streaming(FakeAgent(), "投诉", hitl=hitl, session_id="s1"))
    handoff = [e for e in events if e["type"] == "handoff"]
    assert len(handoff) == 1
    assert handoff[0]["reasons"]
    assert q.count_pending() == 1
    assert q.get(handoff[0]["handoff_id"])["payload"]["recent_context"]


def test_no_escalation_for_normal(tmp_path):
    hitl, q = _hitl(tmp_path)
    agent = FakeAgent(intent=IntentType.ORDER_QUERY, confidence=0.95, requires_human=False)
    events = list(run_agent_streaming(agent, "查订单", hitl=hitl, session_id="s1"))
    assert not any(e["type"] == "handoff" for e in events)
    assert q.count_pending() == 0


def test_no_hitl_unchanged():
    agent = FakeAgent()
    events = list(run_agent_streaming(agent, "投诉"))
    assert [e["type"] for e in events] == ["reply", "metadata", "done"]
