"""协作总线/共享上下文/触达草稿的数据层测试。"""

import json

import pytest

from app.db.database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    return d


def test_publish_and_claim_event(db):
    eid = db.publish_event("signal.anomaly", {"product_id": "P001"},
                           source_agent="service", target_agent="analyst",
                           correlation_id="C1")
    assert eid > 0
    claimed = db.claim_events("analyst")
    assert len(claimed) == 1
    assert claimed[0]["id"] == eid
    assert claimed[0]["payload"] == {"product_id": "P001"}
    assert claimed[0]["status"] == "processing"


def test_claim_is_idempotent_across_workers(db):
    """同一事件不能被认领两次——否则一条异常会产出两份洞察/两份草稿。"""
    db.publish_event("signal.anomaly", {"x": 1}, "service", "analyst", "C1")
    first = db.claim_events("analyst")
    second = db.claim_events("analyst")
    assert len(first) == 1
    assert second == []


def test_claim_filters_by_target(db):
    db.publish_event("signal.anomaly", {}, "service", "analyst", "C1")
    assert db.claim_events("growth") == []


def test_finish_event_marks_done(db):
    eid = db.publish_event("signal.anomaly", {}, "service", "analyst", "C1")
    db.claim_events("analyst")
    assert db.finish_event(eid, "done") is True
    rows = db.list_events(correlation_id="C1")
    assert rows[0]["status"] == "done"


def test_list_events_by_correlation(db):
    db.publish_event("signal.anomaly", {}, "service", "analyst", "C1")
    db.publish_event("insight.diagnosis", {}, "analyst", "growth", "C1")
    db.publish_event("signal.anomaly", {}, "service", "analyst", "C2")
    assert len(db.list_events(correlation_id="C1")) == 2


def test_bad_payload_row_is_skipped_not_fatal(db):
    """单条脏 JSON 不能拖垮整个消费循环(与 list_skill_traces 同口径)。"""
    db.publish_event("signal.anomaly", {"ok": 1}, "service", "analyst", "C1")
    conn = db.connect()
    try:
        conn.execute("INSERT INTO agent_events (event_type, payload, source_agent, "
                     "target_agent, correlation_id, status, created_at) "
                     "VALUES ('x', '{bad', 's', 'analyst', 'C1', 'pending', '2026-01-01 00:00:00')")
        conn.commit()
    finally:
        conn.close()
    claimed = db.claim_events("analyst")
    assert [c["payload"] for c in claimed] == [{"ok": 1}]


def test_shared_context_roundtrip(db):
    db.set_shared_context("diagnosis:P001", {"cause": "尺码不准"},
                          source_agent="analyst", correlation_id="C1")
    got = db.get_shared_context("diagnosis:P001")
    assert got["value"] == {"cause": "尺码不准"}
    assert got["source_agent"] == "analyst"


def test_shared_context_expires(db):
    db.set_shared_context("k", {"v": 1}, "analyst", "C1", ttl_seconds=-1)
    assert db.get_shared_context("k") is None


def test_shared_context_overwrites_same_key(db):
    db.set_shared_context("k", {"v": 1}, "analyst", "C1")
    db.set_shared_context("k", {"v": 2}, "growth", "C2")
    got = db.get_shared_context("k")
    assert got["value"] == {"v": 2}
    assert got["source_agent"] == "growth"


def test_outreach_draft_lifecycle(db):
    did = db.create_outreach_draft(
        opportunity_type="unpaid_order", user_id="u1", order_id="ORD-1",
        content="亲,这款鞋我们已更新尺码建议", offer={"coupon": "9折"},
        reason="尺码疑虑导致未付款", correlation_id="C1", created_by="growth")
    assert did > 0
    drafts = db.list_outreach_drafts(status="draft")
    assert len(drafts) == 1
    assert drafts[0]["offer"] == {"coupon": "9折"}
    assert db.review_outreach_draft(did, "approved", reviewed_by="admin") is True
    assert db.list_outreach_drafts(status="draft") == []
    assert db.get_outreach_draft(did)["status"] == "approved"


def test_review_only_applies_to_draft_state(db):
    """已审的草稿不能被再审一次——防止重复发送。"""
    did = db.create_outreach_draft("unpaid_order", "u1", "ORD-1", "x", {}, "r", "C1", "growth")
    assert db.review_outreach_draft(did, "approved", "admin") is True
    assert db.review_outreach_draft(did, "rejected", "admin2") is False
