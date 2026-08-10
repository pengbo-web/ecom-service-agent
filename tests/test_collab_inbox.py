"""人工闸待办:投给 `human` 却没有任何消费方的事件。

路由表把 `action.drafts_ready` / `result.outreach_converted` /
`result.outreach_no_change` 都投给 `human`,理由写得清清楚楚("草稿必须经人工
审批才会发出"、"转化结果供人工在工作台查看")。但 `run_collab_cycle` 只
`consume(AGENT_ANALYST)` 与 `consume(AGENT_GROWTH)`——**human 没有任何消费方**,
而在这个入口之前也没有任何界面列出它们。实测积压 325 条,状态永远 pending。

与失败事件曾经的处境完全一样:"留给人工"事实上是"留给没人"。
"""

from __future__ import annotations

import inspect

import pytest

from app.multi_agent import bus


@pytest.fixture()
def db(tmp_path):
    from app.db.database import Database

    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    return d


def test_human_target_has_no_consumer_in_the_worker():
    """把前提钉住:worker 确实不消费 human。

    如果哪天有人给 human 加了消费方,这条会红——那时待办面板的定位要重新想,
    而不是让两套机制同时改同一批事件。
    """
    from app.multi_agent import collab

    src = inspect.getsource(collab.run_once)
    assert "AGENT_ANALYST" in src and "AGENT_GROWTH" in src
    assert "AGENT_HUMAN" not in src


def test_lists_only_pending_events_for_that_target(db):
    db.publish_event("action.drafts_ready", {"drafted": 2}, "growth", bus.AGENT_HUMAN, "C1")
    db.publish_event("signal.anomaly", {}, "service", bus.AGENT_ANALYST, "C1")
    rows = db.list_pending_for_target(bus.AGENT_HUMAN)
    assert [r["event_type"] for r in rows] == ["action.drafts_ready"]
    assert db.count_pending_for_target(bus.AGENT_HUMAN) == 1


def test_ordering_matches_claim_order(db):
    """人看到的顺序要与系统认为的轻重缓急一致。

    一个按时间倒序、一个按优先级,会让"最该先看的"沉到列表底部。
    """
    db.publish_event("a", {}, "s", bus.AGENT_HUMAN, "C1", priority=-10)
    db.publish_event("b", {}, "s", bus.AGENT_HUMAN, "C2", priority=10)
    db.publish_event("c", {}, "s", bus.AGENT_HUMAN, "C3", priority=0)
    assert [r["event_type"] for r in db.list_pending_for_target(bus.AGENT_HUMAN)] == ["b", "c", "a"]


def test_acknowledge_is_idempotent(db):
    eid = db.publish_event("action.drafts_ready", {}, "growth", bus.AGENT_HUMAN, "C1")
    assert db.acknowledge_event(eid) is True
    assert db.acknowledge_event(eid) is False, "连点两次只第一次生效"
    assert db.count_pending_for_target(bus.AGENT_HUMAN) == 0


def test_acknowledge_does_not_touch_other_states(db):
    """只对 pending 生效:不能把别人正在处理(processing)或已失败的事件划掉。"""
    eid = db.publish_event("x", {}, "s", bus.AGENT_ANALYST, "C1")
    claimed = db.claim_events(bus.AGENT_ANALYST, limit=1)
    assert claimed and claimed[0]["id"] == eid
    assert db.acknowledge_event(eid) is False, "processing 的事件不该被人工确认划掉"


def test_finish_event_would_have_silently_failed(db):
    """说明为什么不复用 `finish_event`。

    它只对 `processing` 生效,而人工闸的事件**从来不会被认领**——复用会静默
    返回 False,界面上看起来像"点了没反应"。
    """
    eid = db.publish_event("x", {}, "s", bus.AGENT_HUMAN, "C1")
    assert db.finish_event(eid, "done") is False
    assert db.acknowledge_event(eid) is True


def test_payload_is_decoded(db):
    db.publish_event("result.outreach_converted", {"draft_id": 7, "outcome": "converted"},
                     "analyst", bus.AGENT_HUMAN, "C1")
    row = db.list_pending_for_target(bus.AGENT_HUMAN)[0]
    assert row["payload"]["outcome"] == "converted"


def test_endpoints_are_admin_only():
    from app.api import app as app_module

    src = inspect.getsource(app_module)
    for path in ("/api/admin/collab/inbox", "/api/admin/collab/inbox/{event_id}/ack"):
        idx = src.index(path)
        assert "admin_auth" in src[idx - 200:idx + 200]
