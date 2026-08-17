"""总线层路由表:编排决策从 Expert Agent 里搬出来之后的行为。

钉住的核心性质是**Expert 之间没有一方在指挥另一方**:发布方只宣布"发生了
什么",谁该处理由路由表决定。所以这份测试的重点不是"某条规则的取值",而是
"决策在哪一层"——路由表改了行为就该变,Expert 代码不该有任何转发逻辑。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from app.multi_agent import bus, routing
from app.multi_agent.routing import Subscription, resolve
from app.db.database import Database


@pytest.fixture()
def db(tmp_path):
    from app.db import set_db
    d = Database(db_path=str(tmp_path / "route.db"))
    d.init_schema()
    set_db(d)          # 换全局单例:总线载体在调用时才 get_db(),一处即全覆盖
    yield d
    set_db(None)


# ---------- 路由表本身(纯函数) ----------

def test_every_event_type_is_registered():
    """总线定义的每个事件类型都必须在路由表里有条目——哪怕订阅者只有 human。

    漏登记的后果是静默的:publish 返回 None、事件不落库、时间线上什么都看不到。
    这条测试让"新加一个事件类型却忘了配路由"当场变红,而不是上线后才发现某条
    协作链断在中间。
    """
    declared = {v for k, v in vars(bus).items() if k.startswith("EV_")}
    missing = declared - set(routing.SUBSCRIPTIONS)
    assert not missing, f"这些事件类型没有登记订阅者: {missing}"


def _targets(event_type: str, payload: dict) -> list[str]:
    """只取收件人,忽略优先级——大多数用例关心的是"投给谁"这件事。"""
    return [t for t, _prio in resolve(event_type, payload)]


def test_anomaly_goes_to_analyst():
    assert _targets(bus.EV_SIGNAL_ANOMALY, {"kind": "refund_rate_high"}) == [bus.AGENT_ANALYST]


def test_drafts_ready_always_goes_to_human():
    """草稿必须经人工审批——这条订阅永远无条件,不允许加谓词绕开人工闸。"""
    subs = routing.SUBSCRIPTIONS[bus.EV_DRAFTS_READY]
    assert [s.target for s in subs] == [bus.AGENT_HUMAN]
    assert all(s.when is None for s in subs), "人工闸的订阅不允许带条件"


def test_diagnosis_forwards_to_growth_only_when_worthy():
    """转化相关且归因未降级才唤醒营销。"""
    assert _targets(bus.EV_INSIGHT_DIAGNOSIS,
                    {"kind": "refund_rate_high", "degraded": False}) == [bus.AGENT_GROWTH]


def test_degraded_diagnosis_is_not_forwarded():
    """归因降级时 conclusion 只剩"仅列事实",嵌进买家话术没有意义。"""
    assert _targets(bus.EV_INSIGHT_DIAGNOSIS,
                    {"kind": "refund_rate_high", "degraded": True}) == []


def test_unrelated_anomaly_kind_is_not_forwarded():
    """服务质量类异常(工具失败率等)不该触发推销——推销不解决那类问题。"""
    assert _targets(bus.EV_INSIGHT_DIAGNOSIS,
                    {"kind": "tool_error_rate_high", "degraded": False}) == []


def test_unknown_event_type_returns_empty_not_raises():
    """暂时没人订阅的事件是合法的。报错只会让"引入新事件类型"变成一次故障。"""
    assert _targets("some.brand.new.event", {}) == []


def test_predicate_exception_means_not_delivered(monkeypatch):
    """fail-closed:判不清就不唤醒。

    多唤醒一个 Agent 的代价是它对着半成品数据产出垃圾草稿;漏唤醒的代价只是
    这一轮没跑。两者不对称,所以选不唤醒。
    """
    def _boom(_payload):
        raise RuntimeError("谓词炸了")

    monkeypatch.setitem(routing.SUBSCRIPTIONS, "test.event",
                        [Subscription("nobody", when=_boom)])
    assert _targets("test.event", {}) == []


# ---------- publish 扇出 ----------

def test_publish_without_target_uses_routing_table(db):
    corr = bus.publish(bus.EV_SIGNAL_ANOMALY, {"kind": "refund_rate_high"},
                       bus.AGENT_SERVICE)
    assert corr is not None
    rows = db.list_events(correlation_id=corr)
    assert [r["target_agent"] for r in rows] == [bus.AGENT_ANALYST]


def test_publish_fans_out_to_multiple_subscribers(db, monkeypatch):
    """一条事件多个订阅者 → 插多行,同 correlation_id、不同收件人。

    扇出在**写入时**做,不是消费时反查:后者会直接破坏幂等(status 是行级
    单值,一个 Agent 处理完置 done,另一个就再也捞不到了)。
    """
    monkeypatch.setitem(routing.SUBSCRIPTIONS, bus.EV_SIGNAL_ANOMALY,
                        [Subscription(bus.AGENT_ANALYST), Subscription("inventory")])
    corr = bus.publish(bus.EV_SIGNAL_ANOMALY, {"kind": "refund_rate_high"},
                       bus.AGENT_SERVICE)
    rows = db.list_events(correlation_id=corr)
    assert sorted(r["target_agent"] for r in rows) == ["analyst", "inventory"]
    # 两条投递记录各自独立认领,互不影响
    assert len(db.claim_events(bus.AGENT_ANALYST, limit=10)) == 1
    assert len(db.claim_events("inventory", limit=10)) == 1


def test_new_agent_needs_no_expert_code_change(db, monkeypatch):
    """本方案的核心承诺:加一个 Agent 只改路由表。

    这条测试就是那句承诺的可执行版本——只往 SUBSCRIPTIONS 里加一行,不碰任何
    Expert 的代码,新 Agent 就能收到事件。
    """
    monkeypatch.setitem(
        routing.SUBSCRIPTIONS, bus.EV_INSIGHT_DIAGNOSIS,
        list(routing.SUBSCRIPTIONS[bus.EV_INSIGHT_DIAGNOSIS]) + [Subscription("inventory")])
    corr = bus.publish(bus.EV_INSIGHT_DIAGNOSIS,
                       {"kind": "refund_rate_high", "degraded": False},
                       bus.AGENT_ANALYST)
    targets = {r["target_agent"] for r in db.list_events(correlation_id=corr)}
    assert targets == {bus.AGENT_GROWTH, "inventory"}


def test_publish_with_no_subscriber_returns_none_but_leaves_a_tombstone(db):
    """**行为变了,断言跟着变。** 原来这里断言"什么都不写",而那正是被修掉的缺陷:
    一条链"正当地走到了尽头"与"根本没跑起来"在事件表上长得一模一样(实测 462 条
    tool_error_rate_high 的诊断就是这么隐形的)。

    返回值仍是 None——调用方的语义是"这条链有没有起来",而它确实没起来。墓碑是
    给人看的,不改变调用方的判断。
    """
    assert bus.publish("nobody.subscribes", {}, bus.AGENT_SERVICE) is None
    rows = db.list_events()
    assert len(rows) == 1
    assert rows[0]["status"] == bus.STATUS_NO_SUBSCRIBER
    assert rows[0]["target_agent"] == ""      # 没有收件人,而那正是它要说的事


def test_tombstone_does_not_enter_the_work_queue(db):
    """墓碑不是待办也不是故障:进 claim 会变成永远没人处理的僵尸,进 failed 会把
    "没人订阅"报成故障。两条查询本来就按 status/target 精确作用域,这里钉住它。"""
    bus.publish("nobody.subscribes", {}, bus.AGENT_SERVICE)
    assert db.claim_events("", limit=10) == []
    assert db.claim_events(bus.AGENT_ANALYST, limit=10) == []
    assert db.count_failed_events() == 0
    assert db.reclaim_stale_events(older_than_seconds=0) == 0


def test_explicit_target_bypasses_routing_table(db):
    """逃生口:显式 target 时不查路由表,行为与改造前逐字节一致。"""
    corr = bus.publish("nobody.subscribes", {}, bus.AGENT_SERVICE, target="analyst")
    rows = db.list_events(correlation_id=corr)
    assert [r["target_agent"] for r in rows] == ["analyst"]


def test_publish_is_fail_soft_when_db_breaks(db, monkeypatch):
    """发布点之一在买家会话热路径上:总线不可用绝不能让那一轮失败。"""
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "publish_event", _boom)
    assert bus.publish(bus.EV_SIGNAL_ANOMALY, {"kind": "refund_rate_high"},
                       bus.AGENT_SERVICE) is None      # 不抛


def test_collab_disabled_publishes_nothing(db, monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "collab_enabled", False)
    assert bus.publish(bus.EV_SIGNAL_ANOMALY, {"kind": "refund_rate_high"},
                       bus.AGENT_SERVICE) is None
    assert db.list_events() == []


# ---------- 编排决策确实离开了 Expert ----------

def test_expert_modules_no_longer_decide_recipients():
    """回归:Expert 的代码里不该再出现"决定收件人"的痕迹。

    钉的是**结构**而不是行为:`MARKETING_WORTHY` 这个常量如果在 collab.py
    里复活(哪怕没人引用),下一次改规则就一定有人改错地方——这是本项目
    "手抄一份会漂移"已经发生过 6 次的那类隐患。
    """
    import app.multi_agent.collab as collab
    assert not hasattr(collab, "MARKETING_WORTHY"), \
        "转发规则应只存在于 routing.py,collab.py 里不该有副本"


def test_describe_renders_the_whole_table():
    """路由表是"多 Agent 到底怎么协作"的权威声明,必须能被人读到。"""
    rows = routing.describe()
    assert {r["event_type"] for r in rows} >= set(routing.SUBSCRIPTIONS)
    assert all(r["reason"] for r in rows), "每条订阅都要写明理由"
