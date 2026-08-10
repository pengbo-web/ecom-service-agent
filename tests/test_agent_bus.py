"""协作总线门面:发布 fail-soft、消费幂等、处理器异常隔离。"""

import pytest

from app.db import set_db

from app.multi_agent import bus
from app.db.database import Database


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    set_db(d)   # 换全局单例:总线载体在调用时才 get_db(),一处即全覆盖
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
    # 打在 **bus 自己的命名空间**上:bus 是 `from ... import get_event_bus`,
    # 名字已经绑进本模块,patch 源模块不生效。
    monkeypatch.setattr(bus, "get_event_bus", boom)
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
    """开关关闭时 consume 必须是彻底的空操作——即使库里确实躺着待处理事件。

    事件要在开关关闭**之前**发布,否则 publish 自己的开关检查会先把事件
    拦下来,consume 那边测出 claimed == 0 什么都证明不了(即使 consume
    内部的开关检查被删掉,这条测试照样会通过——测的是空库,不是开关)。
    """
    from app.config import settings as st

    bus.publish(bus.EV_SIGNAL_ANOMALY, {}, bus.AGENT_SERVICE, bus.AGENT_ANALYST)
    assert len(wired.list_events()) == 1  # 确认事件真的落库了,不是被 publish 的开关拦下

    monkeypatch.setattr(st.settings, "collab_enabled", False)
    assert bus.consume(bus.AGENT_ANALYST, lambda ev: None) == {
        "claimed": 0, "done": 0, "failed": 0,
    }
    # 事件仍原样躺在 pending,证明 consume 真的没碰它(而不只是巧合地没事可claim)
    assert wired.list_events()[0]["status"] == "pending"


def test_consume_counts_persist_failure_when_finish_event_raises(wired, monkeypatch):
    """finish_event 本身炸了(锁表/磁盘满等)不能让异常逃出 consume,也不能
    被计入 done——处理器确实跑完了,但落库没确认,不能当成"完成"来算。"""
    bus.publish(bus.EV_SIGNAL_ANOMALY, {"i": 1}, bus.AGENT_SERVICE, bus.AGENT_ANALYST)
    bus.publish(bus.EV_SIGNAL_ANOMALY, {"i": 2}, bus.AGENT_SERVICE, bus.AGENT_ANALYST)

    original_finish = wired.finish_event
    calls = []

    def flaky_finish(event_id, status):
        calls.append(event_id)
        if len(calls) == 1:
            raise RuntimeError("db locked")
        return original_finish(event_id, status)

    monkeypatch.setattr(wired, "finish_event", flaky_finish)

    stats = bus.consume(bus.AGENT_ANALYST, lambda ev: None)

    assert stats["claimed"] == 2
    assert stats["done"] == 1
    assert stats["failed"] == 0
    assert stats["persist_failed"] == 1
    # 第一条 finish_event 落库失败,状态应仍停在 processing(没被悄悄记成 done)
    statuses = sorted(e["status"] for e in wired.list_events())
    assert statuses == ["done", "processing"]


def test_consume_counts_persist_failure_when_finish_event_returns_false(wired, monkeypatch):
    """finish_event 返回 False(行已不在 processing,比如被并发的
    reclaim_stale_events 抢先收回)同样不能被计成 done。"""
    bus.publish(bus.EV_SIGNAL_ANOMALY, {}, bus.AGENT_SERVICE, bus.AGENT_ANALYST)
    monkeypatch.setattr(wired, "finish_event", lambda event_id, status: False)

    stats = bus.consume(bus.AGENT_ANALYST, lambda ev: None)

    assert stats == {"claimed": 1, "done": 0, "failed": 0, "persist_failed": 1}


def test_publish_treats_empty_correlation_id_as_absent(wired):
    """correlation_id="" 与"未传"同等对待:生成新 id,而不是原样透传空串。
    这是刻意选择的行为(``correlation_id or new_correlation_id()``),这里
    钉住它,以防日后有人"顺手"改成只判断 is None。"""
    corr = bus.publish(bus.EV_SIGNAL_ANOMALY, {}, bus.AGENT_SERVICE,
                       bus.AGENT_ANALYST, correlation_id="")
    assert corr != ""
    assert corr and corr.startswith("C")
