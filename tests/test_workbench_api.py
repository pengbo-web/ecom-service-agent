from fastapi.testclient import TestClient
from app.api.app import create_app
from app.api.session_manager import SessionManager


def _client():
    app = create_app(session_manager=SessionManager())
    return TestClient(app)


def test_admin_conversations_lists_across_users():
    c = _client()
    # 两个不同客户各开一个会话
    c.post("/api/users", json={"user_id": "alice", "name": "Alice"})
    c.post("/api/users", json={"user_id": "bob", "name": "Bob"})
    r1 = c.post("/api/conversation/open", json={"user_id": "alice"},
                headers={"Authorization": "Bearer " + c.post("/api/auth/login", json={"user_id": "alice"}).json()["token"]})
    r2 = c.post("/api/conversation/open", json={"user_id": "bob"},
                headers={"Authorization": "Bearer " + c.post("/api/auth/login", json={"user_id": "bob"}).json()["token"]})
    assert r1.status_code == 200 and r2.status_code == 200

    resp = c.get("/api/admin/conversations")
    assert resp.status_code == 200
    users = {row["user_id"] for row in resp.json()["conversations"]}
    assert {"alice", "bob"} <= users
