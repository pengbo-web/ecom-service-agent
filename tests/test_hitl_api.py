from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.observability.store import TraceStore
from app.hitl.queue import HandoffQueue
from app.hitl.manual_mode import ManualMode
from app.hitl.manager import HitlManager
from app.schemas.response import CustomerServiceResponse, IntentType


class FakeAgent:
    def __init__(self, session_path=None):
        self.event_sink = None
        self.client = None
        self.raw_messages = []
        self.chat_called = False

    def chat(self, user_input):
        self.chat_called = True
        return CustomerServiceResponse(
            intent=IntentType.GREETING, confidence=0.9, reply="您好",
            requires_human=False, follow_up_question=None,
        )


def _client(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db")); store.init_schema()
    q = HandoffQueue(str(tmp_path / "h.db")); q.init_schema()
    q.add({"session_id": "s1", "intent": "complaint", "reasons": ["敏感意图"]})
    mm = ManualMode(3600)
    hitl = HitlManager(q, mm, 0.6)
    mgr = SessionManager(agent_factory=lambda p: FakeAgent(p))
    app = create_app(session_manager=mgr, trace_store=store, hitl=hitl)
    return TestClient(app), q, mm


def test_list_and_resolve_handoffs(tmp_path):
    c, q, _ = _client(tmp_path)
    lst = c.get("/api/handoffs").json()
    assert len(lst) == 1
    hid = lst[0]["handoff_id"]
    assert c.post(f"/api/handoffs/{hid}/resolve").json()["status"] == "resolved"
    assert c.get("/api/handoffs").json() == []


def test_takeover_toggle(tmp_path):
    c, q, mm = _client(tmp_path)
    r = c.post("/api/session/s1/takeover").json()
    assert r["mode"] == "manual"
    assert mm.is_manual("s1") is True
    r2 = c.post("/api/session/s1/takeover").json()
    assert r2["mode"] == "auto"


def test_manual_mode_short_circuits_chat(tmp_path):
    c, q, mm = _client(tmp_path)
    mm.enter("s1")
    resp = c.post("/api/chat", json={"session_id": "s1", "message": "在吗"})
    assert "人工" in resp.text           # 返回人工处理中提示
