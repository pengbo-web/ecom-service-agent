"""R5:会话冷归档——会话被回收时完整落到 SQLite session_archive(审计/离线分析)。"""

import json

import pytest

from app.db.database import Database
from app.db import set_db
from app.session.archive import SqliteSessionArchiver, NullSessionArchiver
from app.api.session_manager import SessionManager


@pytest.fixture
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    set_db(d)
    return d


class FakeAgent:
    def __init__(self, session_path):
        self.session_path = session_path
        self.user_id = "alice"
        self.summary = None
        self.raw_messages = [{"role": "user", "content": "查订单"},
                             {"role": "assistant", "content": "已为您查询"}]
    def save(self): pass
    def close(self): pass


def test_archiver_writes_row(db):
    a = FakeAgent("x.json")
    SqliteSessionArchiver().archive("s1", a)
    row = db.get_archived_session("s1")
    assert row is not None
    assert row["user_id"] == "alice" and row["msg_count"] == 2
    assert json.loads(row["messages"])[0]["content"] == "查订单"


def test_null_archiver_noop(db):
    NullSessionArchiver().archive("s2", FakeAgent("x.json"))
    assert db.get_archived_session("s2") is None


def test_reaper_archives_on_evict(db):
    clock = type("C", (), {"t": 1000.0})()
    mgr = SessionManager(
        agent_factory=lambda p, u=None: FakeAgent(p),
        clock=lambda: clock.t,
        archiver=SqliteSessionArchiver(),
    )
    mgr.get_or_create("s3")
    clock.t = 1000 + 400          # 空闲超时
    reaped = mgr.sweep(idle_ttl=300)
    assert reaped == ["s3"]
    # 回收时归档:session_archive 有该会话
    assert db.get_archived_session("s3")["user_id"] == "alice"


def test_reaper_without_archiver_does_not_write(db):
    clock = type("C", (), {"t": 1000.0})()
    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p), clock=lambda: clock.t)
    mgr.get_or_create("s4")
    clock.t += 400
    mgr.sweep(idle_ttl=300)
    assert db.get_archived_session("s4") is None   # 默认 NullArchiver 不归档
