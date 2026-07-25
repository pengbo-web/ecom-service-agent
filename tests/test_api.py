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
    assert types == ["conversation", "thought", "reply", "metadata", "done"]
    assert events[2]["content"] == "收到：查一下订单"


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


def test_chat_emits_conversation_event_and_rotates_closed(tmp_path, monkeypatch):
    # 隔离:app.py 内的 get_db() 与本测试直接调用的 get_db() 是同一个函数对象，
    # 都读同一个 app.db._DB 全局单例；直接换掉这个单例即可让二者一致指向临时库。
    from app.db import Database
    temp_db = Database(str(tmp_path / "t.db"))
    temp_db.init_schema()
    monkeypatch.setattr("app.db._DB", temp_db)

    client, _ = _client()
    cid = client.post("/api/conversation/open", json={"user_id": "u9"}).json()["conversation_id"]
    # open 会话:首帧 conversation,status=active,ID 不变
    r = client.post("/api/chat", json={"session_id": cid, "message": "你好", "user_id": "u9"})
    events = _parse_sse(r.text)
    conv = next(e for e in events if e["type"] == "conversation")
    assert conv["conversation_id"] == cid and conv["status"] == "active"
    # 关闭后再发:换发新 ID,status=rotated
    from app.db import get_db
    get_db().close_conversation(cid, "manual")
    r2 = client.post("/api/chat", json={"session_id": cid, "message": "在吗", "user_id": "u9"})
    conv2 = next(e for e in _parse_sse(r2.text) if e["type"] == "conversation")
    assert conv2["status"] == "rotated" and conv2["conversation_id"] != cid


def test_reset_rotates_conversation():
    client, _ = _client()
    cid = client.post("/api/conversation/open", json={"user_id": "u9"}).json()["conversation_id"]
    r = client.post("/api/session/reset", json={"session_id": cid})
    body = r.json()
    assert body["conversation_id"] != cid
    from app.db import get_db
    assert get_db().get_conversation(cid)["close_reason"] == "reset"


def test_history_falls_back_to_snapshot_when_hot_empty(tmp_path, monkeypatch):
    from app.db import Database
    temp_db = Database(str(tmp_path / "t.db"))
    temp_db.init_schema()
    monkeypatch.setattr("app.db._DB", temp_db)

    client, mgr = _client()   # + 临时 db monkeypatch(同 auth 测试)
    from app.db import get_db
    from app.config.settings import settings
    monkeypatch.setattr(settings, "auth_enabled", False)   # 先测纯 fallback 逻辑
    # 造:conversations 有归属 + 快照有内容 + 热存储无(peek_messages 返回 [])
    get_db().create_conversation("u1")   # 忽略返回,单独建一条已知 id
    cid = "c-snaponly"
    import sqlite3
    conn = sqlite3.connect(get_db().db_path)
    conn.execute("INSERT INTO conversations (conversation_id, user_id, status, created_at) "
                 "VALUES (?,?,?,?)", (cid, "u1", "closed", "t"))
    conn.commit(); conn.close()
    get_db().upsert_session_snapshot(cid, "u1",
        [{"role": "user", "content": "历史消息"}, {"role": "assistant", "content": "历史回复"}], None)
    r = client.get(f"/api/session/{cid}/history")
    turns = r.json()["turns"]
    assert turns and any("历史消息" in str(t) for t in turns)   # 热为空,读到了快照


def test_history_ownership_still_enforced_on_snapshot(tmp_path, monkeypatch):
    """auth 开时,别人的快照读不到(归属二次校验)。"""
    from app.db import Database
    temp_db = Database(str(tmp_path / "t.db"))
    temp_db.init_schema()
    monkeypatch.setattr("app.db._DB", temp_db)

    client, _ = _client()   # + 临时 db
    from app.db import get_db
    from app.config.settings import settings
    monkeypatch.setattr(settings, "auth_enabled", True)
    # alice 建会话+快照
    tok_a = client.post("/api/users", json={"user_id": "alice", "name": "alice"}).json()["token"]
    ha = {"Authorization": f"Bearer {tok_a}"}
    cid = client.post("/api/conversation/open", headers=ha, json={}).json()["conversation_id"]
    get_db().upsert_session_snapshot(cid, "alice", [{"role": "user", "content": "私密"}], None)
    tok_b = client.post("/api/users", json={"user_id": "bob", "name": "bob"}).json()["token"]
    hb = {"Authorization": f"Bearer {tok_b}"}
    assert client.get(f"/api/session/{cid}/history", headers=hb).status_code == 403
