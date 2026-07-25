"""会话冷快照:upsert 语义 + 读取。临时库,全离线。"""

from app.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    return db


def test_upsert_creates_then_overwrites(tmp_path):
    db = _db(tmp_path)
    db.upsert_session_snapshot("c-1", "u1", [{"role": "user", "content": "第一次"}], None)
    db.upsert_session_snapshot("c-1", "u1",
        [{"role": "user", "content": "第一次"}, {"role": "assistant", "content": "回复"}], "摘要")
    snap = db.get_session_snapshot("c-1")
    assert snap["user_id"] == "u1"
    assert len(snap["messages"]) == 2 and snap["summary"] == "摘要"    # 覆盖为最新,非追加
    # 只留一行(upsert 不堆积)
    import sqlite3
    conn = sqlite3.connect(db.db_path)
    n = conn.execute("SELECT COUNT(*) FROM session_snapshots WHERE session_id='c-1'").fetchone()[0]
    conn.close()
    assert n == 1


def test_get_missing_returns_none(tmp_path):
    assert _db(tmp_path).get_session_snapshot("c-none") is None


def test_messages_roundtrip_as_list(tmp_path):
    db = _db(tmp_path)
    msgs = [{"role": "user", "content": "查订单"}, {"role": "tool", "content": '{"ok":true}'}]
    db.upsert_session_snapshot("c-2", "u1", msgs, None)
    got = db.get_session_snapshot("c-2")
    assert isinstance(got["messages"], list) and got["messages"][1]["role"] == "tool"


def test_snapshot_table_independent_of_archive(tmp_path):
    """快照与审计归档互不干扰。"""
    db = _db(tmp_path)
    db.upsert_session_snapshot("c-3", "u1", [{"role": "user", "content": "x"}], None)
    db.archive_session("c-3", "u1", [{"role": "user", "content": "x"}], None)
    assert db.get_session_snapshot("c-3") is not None
    assert db.get_archived_session("c-3") is not None      # 两张表各存各的


def test_ecomagent_save_writes_snapshot(tmp_path, monkeypatch):
    from app.agent.chat import EcomAgent
    from app.config.settings import settings
    from app.db import Database, set_db
    import types as _t

    db = Database(str(tmp_path / "t.db")); db.init_schema()
    set_db(db)
    monkeypatch.setattr(settings, "session_snapshot_enabled", True)
    try:
        a = EcomAgent.__new__(EcomAgent)
        a.session_path = str(tmp_path / "c-abc.json")
        a.user_id = "u1"
        a.raw_messages = [{"role": "user", "content": "你好"},
                          {"role": "assistant", "content": "您好"}]
        a.summary = None
        a._write_snapshot()
        snap = db.get_session_snapshot("c-abc")     # stem 作 session_id
        assert snap is not None and snap["user_id"] == "u1" and len(snap["messages"]) == 2
    finally:
        set_db(None)


def test_snapshot_skipped_for_default_session_name(tmp_path, monkeypatch):
    from app.agent.chat import EcomAgent
    from app.config.settings import settings
    from app.db import Database, set_db

    db = Database(str(tmp_path / "t.db")); db.init_schema()
    set_db(db)
    monkeypatch.setattr(settings, "session_snapshot_enabled", True)
    try:
        a = EcomAgent.__new__(EcomAgent)
        a.session_path = str(tmp_path / "session.json")   # 默认名 → 跳过
        a.user_id = "u1"; a.raw_messages = [{"role": "user", "content": "x"}]; a.summary = None
        a._write_snapshot()
        assert db.get_session_snapshot("session") is None
    finally:
        set_db(None)


def test_snapshot_disabled_writes_nothing(tmp_path, monkeypatch):
    from app.agent.chat import EcomAgent
    from app.config.settings import settings
    from app.db import Database, set_db

    db = Database(str(tmp_path / "t.db")); db.init_schema()
    set_db(db)
    monkeypatch.setattr(settings, "session_snapshot_enabled", False)
    try:
        a = EcomAgent.__new__(EcomAgent)
        a.session_path = str(tmp_path / "c-xyz.json"); a.user_id = "u1"
        a.raw_messages = [{"role": "user", "content": "x"}]; a.summary = None
        a._write_snapshot()
        assert db.get_session_snapshot("c-xyz") is None
    finally:
        set_db(None)
