import json as _json

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


def test_admin_read_any_session_messages():
    c = _client()
    c.post("/api/users", json={"user_id": "carol", "name": "Carol"})
    tok = c.post("/api/auth/login", json={"user_id": "carol"}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    sid = c.post("/api/conversation/open", json={"user_id": "carol"}, headers=h).json()["conversation_id"]
    # 坐席可读该会话消息(即便自身非该客户),返回结构正确
    r = c.get(f"/api/admin/session/{sid}/messages")
    assert r.status_code == 200
    assert r.json()["session_id"] == sid
    assert isinstance(r.json()["turns"], list)


def test_admin_human_reply_appends_bubble_and_sets_manual():
    c = _client()
    c.post("/api/users", json={"user_id": "dave", "name": "Dave"})
    tok = c.post("/api/auth/login", json={"user_id": "dave"}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    sid = c.post("/api/conversation/open", json={"user_id": "dave"}, headers=h).json()["conversation_id"]

    r = c.post(f"/api/admin/session/{sid}/reply", json={"text": "您好,人工客服为您处理"})
    assert r.status_code == 200
    turns = r.json()["turns"]
    assert turns and turns[-1] == {"role": "assistant", "content": "您好,人工客服为您处理"}

    # 该会话已转人工:客户再发消息被短路(不调 AI)
    stream = c.post("/api/chat", json={"session_id": sid, "message": "在吗", "user_id": "dave"}, headers=h)
    assert stream.status_code == 200
    assert "人工客服" in stream.text


def test_demo_injection_only_targets_demo_user(monkeypatch):
    from app.config.settings import settings as S
    monkeypatch.setattr(S, "demo_mode", True)
    monkeypatch.setattr(S, "demo_hmdp_user_id", "1")
    c = _client()
    # 非 demo 客户 alice 聊天:不应被注入 hmdp_token 塌缩成 "1"
    c.post("/api/users", json={"user_id": "alice2", "name": "A"})
    tok = c.post("/api/auth/login", json={"user_id": "alice2"}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    sid = c.post("/api/conversation/open", json={"user_id": "alice2"}, headers=h).json()["conversation_id"]
    c.post("/api/chat", json={"session_id": sid, "message": "你好", "user_id": "alice2"}, headers=h)
    conv = c.get(f"/api/admin/conversations").json()["conversations"]
    # alice2 的会话仍归属 alice2(未被改写成 "1")
    assert any(row["user_id"] == "alice2" for row in conv)
