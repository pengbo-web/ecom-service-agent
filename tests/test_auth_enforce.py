"""U3:7 端点身份从 token 解出 + history 归属校验。fixture 手法照搬 test_users_api。"""

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.config.settings import settings
from app.db import Database
from app.schemas.response import CustomerServiceResponse, IntentType


class FakeAgent:
    def __init__(self, session_path):
        self.session_path = session_path
        self.event_sink = None
        self.reset_called = False
        self.raw_messages = []

    def chat(self, user_input):
        if self.event_sink:
            self.event_sink({"type": "thought", "content": "思考中"})
        return CustomerServiceResponse(
            intent=IntentType.GREETING, confidence=0.8,
            reply=f"收到：{user_input}", requires_human=False, follow_up_question=None,
        )

    def reset(self):
        self.reset_called = True


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    monkeypatch.setattr("app.api.app.get_db", lambda: db)
    monkeypatch.setattr(settings, "auth_enabled", True)
    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p))
    return TestClient(create_app(session_manager=mgr))


def _login(client, uid):
    client.post("/api/users", json={"user_id": uid, "name": uid})
    tok = client.post("/api/auth/login", json={"user_id": uid}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def test_chat_requires_token_when_enabled(client):
    r = client.post("/api/chat", json={"session_id": "x", "message": "hi", "user_id": "u1"})
    assert r.status_code == 401


def test_chat_identity_comes_from_token_not_body(client, tmp_path):
    h = _login(client, "alice")
    r = client.post("/api/chat", headers=h,
                    json={"session_id": "x", "message": "hi", "user_id": "bob"})   # 冒充 bob
    assert r.status_code == 200
    from app.db import get_db  # 注:fixture 已 monkeypatch,此处取 fixture 的临时库
    # 新开的会话归属 alice(token 用户),不是 bob
    convs = client.get("/api/conversations", headers=h).json()["conversations"]
    assert convs and all(c["user_id"] == "alice" for c in convs)


def test_conversations_list_ignores_query_user(client):
    h = _login(client, "alice")
    client.post("/api/conversation/open", headers=h, json={"user_id": "bob"})
    r = client.get("/api/conversations", params={"user_id": "bob"}, headers=h)
    assert all(c["user_id"] == "alice" for c in r.json()["conversations"])


def test_history_ownership(client):
    ha = _login(client, "alice")
    hb = _login(client, "bob2")
    cid = client.post("/api/conversation/open", headers=ha, json={}).json()["conversation_id"]
    assert client.get(f"/api/session/{cid}/history", headers=ha).status_code == 200
    assert client.get(f"/api/session/{cid}/history", headers=hb).status_code == 403
    assert client.get("/api/session/legacy--abc/history", headers=ha).status_code == 403


def test_memory_uses_token_user(client, monkeypatch):
    h = _login(client, "alice")
    r = client.get("/api/memory", params={"user_id": "bob"}, headers=h)
    assert r.status_code == 200 and r.json()["user_id"] == "alice"


def test_disabled_falls_back_to_self_report(client, monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "auth_enabled", False)
    r = client.post("/api/chat", json={"session_id": "y", "message": "hi", "user_id": "u1"})
    assert r.status_code == 200          # 关门控:无 token 照常(回退现状)
