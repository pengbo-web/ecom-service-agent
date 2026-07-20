from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.hardening.auth import make_admin_auth


def _app(token):
    app = FastAPI()
    auth = make_admin_auth(token)

    @app.get("/admin", dependencies=[Depends(auth)])
    def admin():
        return {"ok": True}

    return TestClient(app)


def test_no_token_allows_all():
    c = _app("")
    assert c.get("/admin").status_code == 200


def test_token_required_when_set():
    c = _app("secret")
    assert c.get("/admin").status_code == 401
    assert c.get("/admin", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert c.get("/admin", headers={"X-Admin-Token": "secret"}).status_code == 200


# ---------- 集成（Task 5 接入后通过）----------
from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.observability.store import TraceStore
from app.schemas.response import CustomerServiceResponse, IntentType


class _FakeAgent:
    def __init__(self, session_path=None):
        self.event_sink = None; self.client = None; self.raw_messages = []
        self.chat_called = False

    def chat(self, user_input):
        self.chat_called = True
        return CustomerServiceResponse(intent=IntentType.ORDER_QUERY, confidence=0.9,
            reply="已发货", requires_human=False, follow_up_question=None)


def _client(tmp_path, **over):
    store = TraceStore(str(tmp_path / "tr.db")); store.init_schema()
    mgr = SessionManager(agent_factory=lambda p: _FakeAgent(p))
    app = create_app(session_manager=mgr, trace_store=store, hitl=None, **over)
    return TestClient(app)


def test_fast_path_skips_agent(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/chat", json={"session_id": "s1", "message": "你好"})
    assert "小夕" in r.text            # 规则秒回
    assert "metadata" not in r.text    # 未走 Agent


def test_admin_endpoint_guarded(tmp_path):
    c = _client(tmp_path, admin_token="secret")
    assert c.get("/api/metrics").status_code == 401
    assert c.get("/api/metrics", headers={"X-Admin-Token": "secret"}).status_code == 200


def test_chat_open_without_admin_token(tmp_path):
    c = _client(tmp_path, admin_token="secret")
    r = c.post("/api/chat", json={"session_id": "s1", "message": "查订单"})
    assert r.status_code == 200
