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
