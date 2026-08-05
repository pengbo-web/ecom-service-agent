"""协作总线/共享上下文/触达草稿的数据层测试。

草稿夹具的 opportunity_type 一律用 `stale_pending_order` —— 生产真正产得出来的
那个值。原先用的 `unpaid_order` 早在 M9 就被证伪并删掉了(这个项目的订单表根本
没有"未支付"状态,催付款是个伪需求),留着它就是又一次"夹具用了生产永远不会产生
的值"——本特性已经被这个模式咬过好几回,不再留第二现场。
"""

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


def test_reclaim_stale_events_returns_to_pending_and_is_reclaimable(db):
    """崩溃的 worker 不该让事件永久卡在 processing——过阈值后应能放回 pending
    并被重新认领。"""
    eid = db.publish_event("signal.anomaly", {"x": 1}, "service", "analyst", "C1")
    claimed = db.claim_events("analyst")
    assert len(claimed) == 1
    # 模拟“认领后 worker 崩溃”:把 consumed_at 拨到很久以前。
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE agent_events SET consumed_at = '2000-01-01 00:00:00' WHERE id = ?",
            (eid,))
        conn.commit()
    finally:
        conn.close()

    reclaimed = db.reclaim_stale_events(older_than_seconds=300)
    assert reclaimed == 1

    row = db.list_events(correlation_id="C1")[0]
    assert row["status"] == "pending"

    reclaim_again = db.claim_events("analyst")
    assert len(reclaim_again) == 1
    assert reclaim_again[0]["id"] == eid
    assert reclaim_again[0]["status"] == "processing"


def test_reclaim_stale_events_never_touches_done_or_failed(db):
    eid_done = db.publish_event("signal.anomaly", {}, "service", "analyst", "C1")
    eid_failed = db.publish_event("signal.anomaly", {}, "service", "analyst", "C2")
    db.claim_events("analyst")
    db.finish_event(eid_done, "done")
    db.finish_event(eid_failed, "failed")

    conn = db.connect()
    try:
        conn.execute(
            "UPDATE agent_events SET consumed_at = '2000-01-01 00:00:00' "
            "WHERE id IN (?, ?)", (eid_done, eid_failed))
        conn.commit()
    finally:
        conn.close()

    reclaimed = db.reclaim_stale_events(older_than_seconds=300)
    assert reclaimed == 0

    statuses = {r["id"]: r["status"] for r in db.list_events(limit=10)}
    assert statuses[eid_done] == "done"
    assert statuses[eid_failed] == "failed"


def test_reclaim_stale_events_leaves_fresh_processing_row_alone(db):
    """刚认领、还在阈值内的 processing 行不该被误回收——worker 可能仍在正常处理。"""
    eid = db.publish_event("signal.anomaly", {}, "service", "analyst", "C1")
    db.claim_events("analyst")

    reclaimed = db.reclaim_stale_events(older_than_seconds=300)
    assert reclaimed == 0

    row = db.list_events(correlation_id="C1")[0]
    assert row["id"] == eid
    assert row["status"] == "processing"


def test_bad_payload_row_logs_a_warning(db, caplog):
    """脏 JSON 行被跳过时不能悄悄消失——必须留下可查的日志。"""
    conn = db.connect()
    try:
        conn.execute("INSERT INTO agent_events (event_type, payload, source_agent, "
                     "target_agent, correlation_id, status, created_at) "
                     "VALUES ('x', '{bad', 's', 'analyst', 'C1', 'pending', '2026-01-01 00:00:00')")
        conn.commit()
    finally:
        conn.close()

    with caplog.at_level("WARNING"):
        claimed = db.claim_events("analyst")

    assert claimed == []
    assert any("agent_events" in r.message or "payload" in r.message
              for r in caplog.records)


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
        opportunity_type="stale_pending_order", user_id="u1", order_id="ORD-1",
        content="亲,这款鞋我们已更新尺码建议", offer={"coupon": "9折"},
        reason="尺码疑虑导致下单后一直没推进", correlation_id="C1", created_by="growth")
    assert did > 0
    drafts = db.list_outreach_drafts(status="draft")
    assert len(drafts) == 1
    assert drafts[0]["offer"] == {"coupon": "9折"}
    assert db.review_outreach_draft(did, "approved", reviewed_by="admin") is True
    assert db.list_outreach_drafts(status="draft") == []
    assert db.get_outreach_draft(did)["status"] == "approved"


def test_review_only_applies_to_draft_state(db):
    """已审的草稿不能被再审一次——防止重复发送。"""
    did = db.create_outreach_draft("stale_pending_order", "u1", "ORD-1", "x", {}, "r", "C1", "growth")
    assert db.review_outreach_draft(did, "approved", "admin") is True
    assert db.review_outreach_draft(did, "rejected", "admin2") is False


def test_mark_outreach_sent_transitions_approved_to_sent(db):
    did = db.create_outreach_draft("stale_pending_order", "u1", "ORD-1", "x", {}, "r", "C1", "growth")
    db.review_outreach_draft(did, "approved", "admin")
    assert db.mark_outreach_sent(did) is True
    assert db.get_outreach_draft(did)["status"] == "sent"


def test_mark_outreach_sent_is_not_idempotent_twice(db):
    """这是拦住"同一条消息发给真实买家两遍"的最后一道闸——必须只成功一次。"""
    did = db.create_outreach_draft("stale_pending_order", "u1", "ORD-1", "x", {}, "r", "C1", "growth")
    db.review_outreach_draft(did, "approved", "admin")
    assert db.mark_outreach_sent(did) is True
    assert db.mark_outreach_sent(did) is False


def test_mark_outreach_sent_rejects_draft_state(db):
    """草稿没经过批准,不能直接跳到已发送。"""
    did = db.create_outreach_draft("stale_pending_order", "u1", "ORD-1", "x", {}, "r", "C1", "growth")
    assert db.mark_outreach_sent(did) is False
    assert db.get_outreach_draft(did)["status"] == "draft"


def test_mark_outreach_sent_rejects_rejected_state(db):
    did = db.create_outreach_draft("stale_pending_order", "u1", "ORD-1", "x", {}, "r", "C1", "growth")
    db.review_outreach_draft(did, "rejected", "admin")
    assert db.mark_outreach_sent(did) is False
    assert db.get_outreach_draft(did)["status"] == "rejected"
