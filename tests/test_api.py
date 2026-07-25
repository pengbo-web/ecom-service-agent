import json

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.schemas.response import CustomerServiceResponse, IntentType


class FakeAgent:
    def __init__(self, session_path):
        self.session_path = session_path
        self.event_sink = None
        self.reset_called = False

    def chat(self, user_input):
        self.event_sink({"type": "thought", "content": "思考中"})
        return CustomerServiceResponse(
            intent=IntentType.GREETING, confidence=0.8,
            reply=f"收到：{user_input}", requires_human=False, follow_up_question=None,
        )

    def reset(self):
        self.reset_called = True


def _client():
    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p))
    return TestClient(create_app(session_manager=mgr)), mgr


def _parse_sse(text):
    return [json.loads(line[len("data: "):])
            for line in text.splitlines() if line.startswith("data: ")]


def test_health():
    client, _ = _client()
    assert client.get("/api/health").json() == {"status": "ok"}


def test_chat_streams_events():
    client, _ = _client()
    # 用非快路径消息，确保走完整 Agent 流程（"你好"会命中规则快路径）
    resp = client.post("/api/chat", json={"session_id": "s1", "message": "查一下订单"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert types == ["thought", "reply", "metadata", "done"]
    assert events[1]["content"] == "收到：查一下订单"


def test_reset_endpoint():
    client, mgr = _client()
    agent = mgr.get_or_create("s1")
    resp = client.post("/api/session/reset", json={"session_id": "s1"})
    assert resp.status_code == 200
    assert agent.reset_called is True


def test_root_serves_html():
    client, _ = _client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_conversation_open_and_list(tmp_path, monkeypatch):
    # 隔离:/api/conversation/open、/api/conversations 内部走 app.api.app.get_db()，
    # 该名字是 `from app.db import get_db` 导入进 app.api.app 模块命名空间的引用；
    # 直接把这个引用换成返回临时库的函数，不碰真正的 ecom.db，也不用管 get_db 的全局单例缓存。
    from app.db import Database
    temp_db = Database(str(tmp_path / "t.db"))
    temp_db.init_schema()
    monkeypatch.setattr("app.api.app.get_db", lambda: temp_db)

    client, _ = _client()
    r1 = client.post("/api/conversation/open", json={"user_id": "u9"})
    assert r1.status_code == 200
    cid = r1.json()["conversation_id"]
    assert cid.startswith("c-")
    r2 = client.post("/api/conversation/open", json={"user_id": "u9"})
    assert r2.json()["conversation_id"] == cid                   # 复用
    r3 = client.get("/api/conversations", params={"user_id": "u9"})
    assert any(c["conversation_id"] == cid for c in r3.json()["conversations"])
