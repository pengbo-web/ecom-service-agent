"""总线规则引擎:事件 Schema + 优先级 + 状态拦截。

这三条是「事件驱动 + 隐式编排」防失控的那一组。松耦合的另一面是失控:发布方
不知道谁在消费、消费方不知道谁在发布,于是"事件长得不对""急事排在闲事后面"
"该停时没停"会同时出现。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config.settings import settings
from app.db.database import Database
from app.multi_agent import bus, event_schema, routing


@pytest.fixture()
def db(tmp_path):
    from app.db import set_db
    d = Database(db_path=str(tmp_path / "rules.db"))
    d.init_schema()
    set_db(d)
    yield d
    set_db(None)


# ---------- S1 事件 Schema ----------

def test_missing_fields_detects_omission():
    assert event_schema.missing_fields(bus.EV_SIGNAL_ANOMALY, {"kind": "x"}) == ["subject"]
    assert event_schema.missing_fields(bus.EV_SIGNAL_ANOMALY,
                                       {"kind": "x", "subject": "P1"}) == []


def test_unknown_event_type_has_no_required_fields():
    """与 routing.resolve 同口径:引入新事件类型不该先变成一次故障。"""
    assert event_schema.missing_fields("brand.new", {}) == []


def test_routing_predicates_only_read_declared_fields():
    """**本组最重要的一条。**

    路由谓词读到的每个 payload 字段,都必须在 REQUIRED_FIELDS 里声明。

    防的是这样一条静默失败链:谓词依赖了一个没人保证会有的字段 → 生产方哪天
    不发它 → 谓词判否 → resolve 返回 [] → publish 返回 None → 整条协作链
    消失,而且没有异常、没有 failed 事件、时间线上什么都没有。失败形态是
    "什么都没发生",是所有形态里最难查的一种。

    实现方式:用一个会记录被访问键的假 payload 去跑每个谓词,再比对声明。
    """
    class _Spy(dict):
        def __init__(self):
            super().__init__()
            self.read: set[str] = set()

        def get(self, key, default=None):
            self.read.add(key)
            return default

    for event_type, subs in routing.SUBSCRIPTIONS.items():
        declared = event_schema.REQUIRED_FIELDS.get(event_type, frozenset())
        for sub in subs:
            for fn in (sub.when, sub.priority):
                if not callable(fn):
                    continue
                spy = _Spy()
                try:
                    fn(spy)
                except Exception:  # noqa: BLE001 谓词对空 payload 抛错不是本条要测的
                    pass
                undeclared = spy.read - set(declared)
                assert not undeclared, (
                    f"{event_type} 的路由谓词读了未声明字段 {undeclared};"
                    f"请补进 event_schema.REQUIRED_FIELDS,否则生产方漏发时会静默断链")


def test_publish_still_delivers_when_fields_missing(db, caplog):
    """缺字段只记 warning、照常发布。

    发布点之一在买家会话的热路径上(转人工埋点),一条埋点的 schema 问题绝不该
    让买家那一轮失败。
    """
    corr = bus.publish(bus.EV_SIGNAL_ANOMALY, {"kind": "refund_rate_high"},
                       bus.AGENT_SERVICE)      # 缺 subject
    assert corr is not None
    assert len(db.list_events(correlation_id=corr)) == 1


# ---------- S2 优先级 ----------

def test_escalation_signal_outranks_routine_scan(db):
    """买家刚被转人工 → 背后有真人在等;例行退款率扫描晚一轮无所谓。"""
    bus.publish(bus.EV_SIGNAL_ANOMALY,
                {"kind": "refund_rate_high", "subject": "P1"}, bus.AGENT_SERVICE)
    bus.publish(bus.EV_SIGNAL_ANOMALY,
                {"kind": "service_escalation", "subject": "s1"}, bus.AGENT_SERVICE)

    claimed = db.claim_events(bus.AGENT_ANALYST, limit=10)
    # 后发的转人工信号排在先发的例行扫描前面
    assert claimed[0]["payload"]["kind"] == "service_escalation"


def test_same_priority_is_still_fifo(db):
    """同优先级严格先来后到——同类事件之间的可预期性不能丢。"""
    for i in range(3):
        bus.publish(bus.EV_SIGNAL_ANOMALY,
                    {"kind": "refund_rate_high", "subject": f"P{i}"}, bus.AGENT_SERVICE)
    claimed = db.claim_events(bus.AGENT_ANALYST, limit=10)
    assert [c["payload"]["subject"] for c in claimed] == ["P0", "P1", "P2"]


def test_priority_predicate_failure_falls_back_to_normal(db, monkeypatch):
    """排序算不出来,不该让事件发不出去。"""
    def _boom(_p):
        raise RuntimeError("优先级谓词炸了")

    monkeypatch.setitem(routing.SUBSCRIPTIONS, bus.EV_SIGNAL_ANOMALY,
                        [routing.Subscription(bus.AGENT_ANALYST, priority=_boom)])
    corr = bus.publish(bus.EV_SIGNAL_ANOMALY, {"kind": "x", "subject": "P1"},
                       bus.AGENT_SERVICE)
    assert corr is not None
    assert db.list_events(correlation_id=corr)[0]["priority"] == routing.P_NORMAL


# ---------- S3 状态拦截(消费闸) ----------

def _pending_handoffs(n: int):
    """把未结工单数打桩成 n。"""
    class _Q:
        def __init__(self, *_a, **_k):
            pass

        def count_pending(self):
            return n

    return patch("app.hitl.queue.HandoffQueue", _Q)


def test_marketing_paused_when_service_is_firefighting(db, monkeypatch):
    """服务侧一堆买家等着人工处理时,不同时推销。"""
    monkeypatch.setattr(settings, "collab_marketing_pause_open_handoffs", 3)
    bus.publish(bus.EV_INSIGHT_DIAGNOSIS,
                {"kind": "refund_rate_high", "degraded": False, "conclusion": "x"},
                bus.AGENT_ANALYST)

    handled = []
    with _pending_handoffs(5):
        stats = bus.consume(bus.AGENT_GROWTH, lambda ev: handled.append(ev))

    assert handled == []                       # 处理器压根没被调用
    assert stats["skipped"] == 1
    assert stats["done"] == 0 and stats["failed"] == 0


def test_skipped_is_neither_done_nor_failed(db, monkeypatch):
    """被拦的事件落 skipped:记 done 是账本撒谎,记 failed 会淹没真正的故障。"""
    monkeypatch.setattr(settings, "collab_marketing_pause_open_handoffs", 3)
    bus.publish(bus.EV_INSIGHT_DIAGNOSIS,
                {"kind": "refund_rate_high", "degraded": False, "conclusion": "x"},
                bus.AGENT_ANALYST)
    with _pending_handoffs(5):
        bus.consume(bus.AGENT_GROWTH, lambda ev: None)

    assert db.count_failed_events() == 0        # 不进失败列表
    assert db.list_events()[0]["status"] == "skipped"
    # 也不会被重新认领(claim 只挑 pending)
    assert db.claim_events(bus.AGENT_GROWTH, limit=10) == []


def test_gate_lets_through_below_threshold(db, monkeypatch):
    monkeypatch.setattr(settings, "collab_marketing_pause_open_handoffs", 10)
    bus.publish(bus.EV_INSIGHT_DIAGNOSIS,
                {"kind": "refund_rate_high", "degraded": False, "conclusion": "x"},
                bus.AGENT_ANALYST)
    handled = []
    with _pending_handoffs(2):
        stats = bus.consume(bus.AGENT_GROWTH, lambda ev: handled.append(ev))
    assert len(handled) == 1 and stats["done"] == 1


def test_gate_is_off_by_default():
    """机制默认在、**策略默认关**。

    "几条未结工单算救火"是业务政策不是技术默认值——同样 5 条,大店是常态、
    小店是事故,替店主拍这个数等于替他做了一个他没同意过的经营决策。

    这条测试还兼防一类具体事故:这个默认值曾经是 5,而闸会去读**真实的**
    hitl.db;开发机上积压的 36 条历史工单把全部协作测试静默拦停,症状只是
    "草稿数为 0",完全看不出跟工单有关。
    """
    from app.config.settings import Settings
    assert Settings().collab_marketing_pause_open_handoffs == 0


def test_threshold_zero_disables_the_gate(db, monkeypatch):
    monkeypatch.setattr(settings, "collab_marketing_pause_open_handoffs", 0)
    bus.publish(bus.EV_INSIGHT_DIAGNOSIS,
                {"kind": "refund_rate_high", "degraded": False, "conclusion": "x"},
                bus.AGENT_ANALYST)
    handled = []
    with _pending_handoffs(999):
        bus.consume(bus.AGENT_GROWTH, lambda ev: handled.append(ev))
    assert len(handled) == 1


def test_gate_failure_is_fail_open(db, monkeypatch):
    """闸自己坏了就放行:它是"少做一点事"的优化,不是安全边界。

    真正的安全边界是人工审批闸,在后面且从不失效。反过来 fail-closed 会让
    一次读库抖动静默吞掉整条协作链。
    """
    monkeypatch.setitem(routing.GATES, bus.AGENT_GROWTH,
                        lambda _ev: (_ for _ in ()).throw(RuntimeError("闸炸了")))
    bus.publish(bus.EV_INSIGHT_DIAGNOSIS,
                {"kind": "refund_rate_high", "degraded": False, "conclusion": "x"},
                bus.AGENT_ANALYST)
    handled = []
    bus.consume(bus.AGENT_GROWTH, lambda ev: handled.append(ev))
    assert len(handled) == 1


def test_analyst_has_no_gate(db):
    """闸只挂在营销侧:参谋是只读的,没有"此刻不该动手"的场景。"""
    assert routing.check_gate(bus.AGENT_ANALYST, {}) == (True, "")


def test_stats_keys_unchanged_when_nothing_skipped(db):
    """没被拦过的轮次,stats 仍只有三个键——既有调用方与测试的字典断言不受影响。"""
    bus.publish(bus.EV_SIGNAL_ANOMALY, {"kind": "refund_rate_high", "subject": "P1"},
                bus.AGENT_SERVICE)
    stats = bus.consume(bus.AGENT_ANALYST, lambda ev: None)
    assert set(stats) == {"claimed", "done", "failed"}
