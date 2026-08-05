"""触达仲裁:正在投诉/人工接管的买家不得被推销;查不清则不发。"""

import pytest

from app.multi_agent import arbitration as arb
from app.db.database import Database


class FakeManual:
    def __init__(self, manual_sessions=()):
        self._m = set(manual_sessions)
    def is_manual(self, session_id):
        return session_id in self._m


class FakeHitl:
    def __init__(self, manual_sessions=()):
        self.manual_mode = FakeManual(manual_sessions)


class FakeQueue:
    def __init__(self, pending=()):
        self._p = list(pending)
    def list_pending(self):
        return [{"session_id": s} for s in self._p]


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    conv = d.create_conversation("u1")
    return d, conv["conversation_id"]


def test_allows_ordinary_buyer(db):
    d, sid = db
    ok, code, reason = arb.check_outreach_allowed("u1", hitl=FakeHitl(), db=d)
    assert ok is True and code == "" and reason == ""


def test_blocks_buyer_in_manual_takeover(db):
    """顾客正等人工处理,同时收到营销话术——这条闸就是为了挡住它。"""
    d, sid = db
    ok, code, reason = arb.check_outreach_allowed("u1", hitl=FakeHitl([sid]), db=d)
    assert ok is False
    assert code == arb.BLOCK_MANUAL
    assert "人工" in reason


def test_blocks_buyer_with_open_handoff(db):
    d, sid = db
    h = FakeHitl()
    h.queue = FakeQueue([sid])
    ok, code, reason = arb.check_outreach_allowed("u1", hitl=h, db=d)
    assert ok is False
    assert code == arb.BLOCK_OPEN_HANDOFF
    assert "工单" in reason


def test_resolved_handoff_does_not_block(db):
    d, sid = db
    h = FakeHitl()
    h.queue = FakeQueue([])          # 已结工单不在 pending 列表里
    ok, code, _ = arb.check_outreach_allowed("u1", hitl=h, db=d)
    assert ok is True and code == ""


def test_buyer_with_no_conversation_is_allowed(db):
    """没有会话的买家谈不上"正在投诉",不该被这条闸拦住(否则新客永远收不到)。"""
    d, _ = db
    ok, code, reason = arb.check_outreach_allowed("nobody", hitl=FakeHitl(), db=d)
    assert ok is True and code == "" and reason == ""


def test_fails_closed_when_state_unreadable(db):
    """查不清状态时必须判不可发——发错的代价远大于漏发。"""
    d, sid = db

    class Boom:
        @property
        def manual_mode(self):
            raise RuntimeError("hitl down")

    ok, code, reason = arb.check_outreach_allowed("u1", hitl=Boom(), db=d)
    assert ok is False
    assert code == arb.BLOCK_UNKNOWN
    assert "无法确认" in reason


def test_no_hitl_configured_is_allowed(db):
    """HITL 整个关掉时不该把营销也锁死(该部署里没有"正在投诉"这个状态)。"""
    d, _ = db
    ok, code, reason = arb.check_outreach_allowed("u1", hitl=None, db=d)
    assert ok is True and code == "" and reason == ""


def test_get_db_failure_fails_closed(monkeypatch):
    """review finding 1:`get_db()` 本身抛异常(库文件打不开等)也必须落在
    fail-closed 分支,而不能让异常从 `check_outreach_allowed` 里逃逸——
    调用方 `approve_draft` 外面没有包 try/except,一旦异常逃逸,操作者看到的
    就是裸 500,而不是"已拒绝发送"。不显式传 db,让函数走内部
    `from app.db import get_db` 的默认路径,再让这条路径本身失败。
    """
    def boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("app.db.get_db", boom)
    ok, code, reason = arb.check_outreach_allowed("u1", hitl=FakeHitl())
    assert ok is False
    assert code == arb.BLOCK_UNKNOWN
    assert "无法确认" in reason
