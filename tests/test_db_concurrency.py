"""数据层并发基座:WAL + busy_timeout。

为什么单独钉住:协作起草并行(collab.handle_insight)与多 worker 进程都依赖
"多个写者能排队而不是立刻报错"。默认 rollback journal 下写者与读者互斥,
第二个写者会拿到 `database is locked`——表现为"一并行就报错"。这份测试保证
这个基座不会在某次重构里被悄悄拿掉。
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app.config.settings import settings
from app.db.database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "conc.db"))
    d.init_schema()
    return d


def test_journal_mode_is_wal(db):
    conn = db.connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_busy_timeout_follows_settings(db, monkeypatch):
    monkeypatch.setattr(settings, "db_busy_timeout_ms", 7000)
    conn = db.connect()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    finally:
        conn.close()


def test_concurrent_writers_do_not_hit_database_is_locked(db):
    """两个线程同时写:必须全部成功,不能出现 `database is locked`。

    这是并行起草的最小前提——每条草稿要写好几次库(落草稿 / 发事件 /
    写共享上下文 / 结束事件),写冲突是常态而不是异常。
    """
    errors: list[Exception] = []
    rows_per_thread = 60

    def _writer(tag: str):
        try:
            for i in range(rows_per_thread):
                db.create_outreach_draft(
                    "unpaid_order", f"{tag}-{i}", f"O-{tag}-{i}", "内容",
                    {}, "", f"C-{tag}-{i}", "growth")
        except Exception as exc:  # noqa: BLE001 收集起来在主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写出错(基座没铺好): {errors[:3]}"
    assert len(db.list_outreach_drafts(status="draft", limit=500)) == rows_per_thread * 2


def test_memory_db_still_works_without_wal(monkeypatch):
    """fail-soft:内存库不支持 WAL,不能因为设不了一个性能 PRAGMA 就整个不可用。"""
    d = Database(db_path=":memory:")
    conn = d.connect()          # 不抛
    try:
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()
