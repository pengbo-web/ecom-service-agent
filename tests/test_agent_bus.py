"""协作总线门面:发布 fail-soft、消费幂等、处理器异常隔离。"""

import pytest

from app.multi_agent import bus
from app.db.database import Database


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    monkeypatch.setattr(bus, "get_db", lambda: d)
    return d


def test_publish_returns_correlation_id(wired):
    corr = bus.publish(bus.EV_SIGNAL_ANOMALY, {"p": 1},
                       bus.AGENT_SERVICE, bus.AGENT_ANALYST)
    assert corr and corr.startswith("C")
    assert len(wired.list_events(correlation_id=corr)) == 1


def test_publish_reuses_given_correlation_id(wired):
    corr = bus.publish(bus.EV_SIGNAL_ANOMALY, {}, bus.AGENT_SERVICE,
                       bus.AGENT_ANALYST, correlation_id="C-FIXED")
    assert corr == "C-FIXED"


def test_publish_is_fail_soft(monkeypatch):
    """总线挂了绝不能让买家那一轮失败——发布异常必须被吞掉。"""
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(bus, "get_db", boom)
    assert bus.publish(bus.EV_SIGNAL_ANOMALY, {}, "service", "analyst") is None


def test_consume_calls_handler_and_marks_done(wired):
    bus.publish(bus.EV_SIGNAL_ANOMALY, {"p": 1}, bus.AGENT_SERVICE, bus.AGENT_ANALYST)
    seen = []
    stats = bus.consume(bus.AGENT_ANALYST, lambda ev: seen.append(ev["payload"]))
    assert stats == {"claimed": 1, "done": 1, "failed": 0}
    assert seen == [{"p": 1}]
    assert wired.list_events()[0]["status"] == "done"


def test_consume_marks_failed_when_handler_raises(wired):
    """一个处理器炸了不能吃掉事件,也不能拖垮同批其它事件。"""
    bus.publish(bus.EV_SIGNAL_ANOMALY, {"i": 1}, bus.AGENT_SERVICE, bus.AGENT_ANALYST)
    bus.publish(bus.EV_SIGNAL_ANOMALY, {"i": 2}, bus.AGENT_SERVICE, bus.AGENT_ANALYST)

    def handler(ev):
        if ev["payload"]["i"] == 1:
            raise ValueError("boom")

    stats = bus.consume(bus.AGENT_ANALYST, handler)
    assert stats == {"claimed": 2, "done": 1, "failed": 1}
    statuses = sorted(e["status"] for e in wired.list_events())
    assert statuses == ["done", "failed"]


def test_second_consume_sees_nothing(wired):
    bus.publish(bus.EV_SIGNAL_ANOMALY, {}, bus.AGENT_SERVICE, bus.AGENT_ANALYST)
    bus.consume(bus.AGENT_ANALYST, lambda ev: None)
    assert bus.consume(bus.AGENT_ANALYST, lambda ev: None)["claimed"] == 0


def test_consume_disabled_by_switch(wired, monkeypatch):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "collab_enabled", False)
    bus.publish(bus.EV_SIGNAL_ANOMALY, {}, bus.AGENT_SERVICE, bus.AGENT_ANALYST)
    assert bus.consume(bus.AGENT_ANALYST, lambda ev: None)["claimed"] == 0
