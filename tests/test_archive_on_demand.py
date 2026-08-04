"""按需冷归档:archive_session_if_changed 去重 + 管理端 /api/admin/sessions/archive。

背景:归档原本只在会话被 30 天 TTL 淘汰时发生,失败驱动的 skill 改进需要
traces 与 archives 相交 —— 等淘汰意味着最快 30 天后才学得到。本测试覆盖:
- Database.archive_session_if_changed 的去重语义
- SqliteSessionArchiver.archive 走去重路径
- 新增的管理端按需归档接口
- SessionManager.snapshot_agents 只读、不改变会话数
"""

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.db import Database
from app.session.archive import SqliteSessionArchiver


def _headers():
    """admin_token 为空时后端不鉴权(见 app/hardening/auth.py),此时不必带头。"""
    from app.config.settings import settings
    return {"X-Admin-Token": settings.admin_token} if settings.admin_token else {}


# ---------- Database.archive_session_if_changed ----------

def test_first_call_writes_and_returns_true(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()

    ok = db.archive_session_if_changed("s1", "u1", [{"role": "user", "content": "hi"}], None)

    assert ok is True
    row = db.get_archived_session("s1")
    assert row is not None
    assert row["msg_count"] == 1


def test_identical_second_call_returns_false_and_adds_no_row(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    msgs = [{"role": "user", "content": "hi"}]
    db.archive_session_if_changed("s1", "u1", msgs, None)

    ok = db.archive_session_if_changed("s1", "u1", msgs, None)

    assert ok is False
    conn = db.connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM session_archive WHERE session_id = ?", ("s1",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_more_messages_returns_true_and_adds_row(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    db.archive_session_if_changed("s1", "u1", [{"role": "user", "content": "hi"}], None)

    grown = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "你好"}]
    ok = db.archive_session_if_changed("s1", "u1", grown, None)

    assert ok is True
    conn = db.connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM session_archive WHERE session_id = ?", ("s1",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 2
    assert db.get_archived_session("s1")["msg_count"] == 2


def test_empty_messages_returns_false_and_writes_nothing(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()

    ok = db.archive_session_if_changed("s1", "u1", [], None)

    assert ok is False
    assert db.get_archived_session("s1") is None


# ---------- SqliteSessionArchiver.archive 走去重路径 ----------

class _FakeAgentForArchive:
    def __init__(self, messages):
        self.user_id = "u1"
        self.raw_messages = messages
        self.summary = None


def test_archiver_archive_dedups_on_repeat_calls(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    monkeypatch.setattr("app.db.get_db", lambda: db)

    archiver = SqliteSessionArchiver()
    agent = _FakeAgentForArchive([{"role": "user", "content": "hi"}])

    archiver.archive("s1", agent)
    archiver.archive("s1", agent)   # 同样内容,第二次应被去重跳过

    conn = db.connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM session_archive WHERE session_id = ?", ("s1",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


# ---------- 管理端按需归档接口 ----------

class _FakeAgent:
    def __init__(self, session_path=None, user_id=None):
        self.user_id = user_id or "default"
        self.raw_messages = []
        self.summary = None


def _client_with_live_sessions(tmp_path):
    mgr = SessionManager(agent_factory=lambda p, u=None: _FakeAgent(p, u))
    agent1 = mgr.get_or_create("s1", "u1")
    agent1.raw_messages.extend([
        {"role": "user", "content": "订单在哪"},
        {"role": "assistant", "content": "已发货"},
    ])
    agent2 = mgr.get_or_create("s2", "u2")
    agent2.raw_messages.append({"role": "user", "content": "你好"})

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()

    app = create_app(session_manager=mgr)
    import app.api.app as app_module
    monkeypatch_target = app_module
    return TestClient(app), mgr, db, monkeypatch_target


def test_admin_endpoint_archives_live_sessions(tmp_path, monkeypatch):
    client, mgr, db, app_module = _client_with_live_sessions(tmp_path)
    monkeypatch.setattr(app_module, "get_db", lambda: db)

    resp = client.post("/api/admin/sessions/archive", headers=_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["archived"] == 2
    assert set(data["archived_sessions"]) == {"s1", "s2"}

    assert db.get_archived_session("s1")["msg_count"] == 2
    assert db.get_archived_session("s2")["msg_count"] == 1


def test_admin_endpoint_is_idempotent_on_unchanged_sessions(tmp_path, monkeypatch):
    client, mgr, db, app_module = _client_with_live_sessions(tmp_path)
    monkeypatch.setattr(app_module, "get_db", lambda: db)

    first = client.post("/api/admin/sessions/archive", headers=_headers()).json()
    second = client.post("/api/admin/sessions/archive", headers=_headers()).json()

    assert first["archived"] == 2
    assert second["archived"] == 0
    assert second["skipped"] == 2


# ---------- SessionManager.snapshot_agents ----------

def test_snapshot_agents_read_only():
    mgr = SessionManager(agent_factory=lambda p, u=None: _FakeAgent(p, u))
    mgr.get_or_create("s1", "u1")
    mgr.get_or_create("s2", "u2")

    snap = mgr.snapshot_agents()

    assert {sid for sid, _ in snap} == {"s1", "s2"}
    assert len(mgr._agents) == 2   # 未发生变更
