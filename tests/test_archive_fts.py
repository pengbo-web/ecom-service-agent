"""WS2:session_archive FTS5 全文召回源(技术方案 §3)。

守四条:分词命中(关键词源捞不到的形态)、三态可区分(unavailable ≠ 没命中)、
卖家侧硬隔离、版本戳换了就整体重建。
"""

import pytest

from app.agent.skills import archive_fts
from app.db.database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "fts.db"))
    d.init_schema()
    return d


def _archive(db, sid, first_user, summary=None):
    db.archive_session_if_changed(
        sid, "u1", [{"role": "user", "content": first_user}], summary)


def test_search_hits_segmented_chinese(db):
    _archive(db, "s1", "鞋子穿着挤脚想换大一码", summary="退换尺码咨询")
    _archive(db, "s2", "优惠券在哪里领")
    assert archive_fts.ensure_index(db) == archive_fts.STATE_OK
    ids, state, reason = archive_fts.search_sessions_with_state(db, "挤脚 换码")
    assert state == archive_fts.STATE_OK
    assert ids == ["s1"]


def test_miss_and_blank_query(db):
    _archive(db, "s1", "退货运费谁出")
    archive_fts.ensure_index(db)
    assert archive_fts.search_sessions(db, "发票") == []
    assert archive_fts.search_sessions(db, "   ") == []


def test_seller_actor_hard_isolated(db):
    """归档只有买家会话:卖家侧全文源在入口就返回空+理由,纪律代码化。"""
    _archive(db, "s1", "我要退货,订单号 ORD-20240115-001")
    ids, state, reason = archive_fts.search_sessions_with_state(db, "退货", actor="seller")
    assert ids == []
    assert reason


def test_version_bump_triggers_rebuild(db):
    _archive(db, "s1", "挤脚")
    archive_fts.ensure_index(db)
    conn = db.connect()
    try:
        conn.execute("UPDATE archive_fts_meta SET value='old-cut' "
                     "WHERE key='segmenter_version'")
        conn.commit()
    finally:
        conn.close()
    assert archive_fts.ensure_index(db) == archive_fts.STATE_OK
    assert archive_fts.search_sessions(db, "挤脚") == ["s1"]


def test_incremental_index_after_ensure(db):
    archive_fts.ensure_index(db)
    msgs = [{"role": "user", "content": "新归档的挤脚咨询"}]
    db.archive_session_if_changed("s9", "u1", msgs, None)
    assert archive_fts.index_session(db, "s9", msgs, None) is True
    assert "s9" in archive_fts.search_sessions(db, "挤脚")


def test_build_failure_reports_unavailable(db, monkeypatch):
    """索引构建炸了 → unavailable,不许半建半留、不许静默空。"""
    _archive(db, "s1", "挤脚")

    def boom(text):
        raise RuntimeError("jieba gone")

    monkeypatch.setattr(archive_fts, "segment", boom)
    assert archive_fts.ensure_index(db) == archive_fts.STATE_UNAVAILABLE
    ids, state, reason = archive_fts.search_sessions_with_state(db, "挤脚")
    assert ids == []
    assert state == archive_fts.STATE_UNAVAILABLE
    assert reason


def test_archiver_hook_is_fail_soft(tmp_path, monkeypatch):
    """归档旁路索引炸了,归档主流程不受影响(SqliteSessionArchiver 同姿态)。"""
    from app.session.archive import SqliteSessionArchiver

    d = Database(db_path=str(tmp_path / "hook.db"))
    d.init_schema()
    monkeypatch.setattr("app.db.get_db", lambda: d)

    def boom(*a, **k):
        raise RuntimeError("index down")

    monkeypatch.setattr("app.agent.skills.archive_fts.index_session", boom)

    class _Agent:
        session_id = "s1"
        user_id = "u1"
        raw_messages = [{"role": "user", "content": "挤脚"}]
        summary = None

    SqliteSessionArchiver().archive("s1", _Agent())   # 不许冒泡
    assert d.list_recent_archives(limit=5)            # 归档照落
