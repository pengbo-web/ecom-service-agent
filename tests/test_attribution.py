"""触达归因:发送时记基线,到期判定是否推进,统计可衡量。"""

import pytest

from app.db.database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    return d


def _sent_draft(d, oid="O1", uid="u1", status_at_send="unpaid", hours_ago=48):
    did = d.create_outreach_draft("unpaid_order", uid, oid, "催一下", {}, "未支付",
                                  "C1", "growth")
    d.review_outreach_draft(did, "approved", "admin")
    d.mark_outreach_sent(did)
    d.set_outreach_baseline(did, status_at_send)
    conn = d.connect()
    try:
        conn.execute("UPDATE outreach_drafts SET sent_at = datetime('now','-'||?||' hours') "
                     "WHERE id = ?", (hours_ago, did))
        conn.commit()
    finally:
        conn.close()
    return did


def test_baseline_recorded(db):
    did = _sent_draft(db)
    assert db.get_outreach_draft(did)["status_at_send"] == "unpaid"
    assert db.get_outreach_draft(did)["outcome"] == "pending"


def test_fresh_draft_not_yet_attributable(db):
    """刚发出去就判定不公平——要给买家反应时间。"""
    _sent_draft(db, hours_ago=1)
    assert db.pending_attribution(older_than_hours=24) == []


def test_stale_draft_is_attributable(db):
    did = _sent_draft(db, hours_ago=48)
    assert [d["id"] for d in db.pending_attribution(older_than_hours=24)] == [did]


def test_order_progress_counts_as_converted(db):
    from app.scripts.attribute_outreach import attribute_once
    did = _sent_draft(db, status_at_send="unpaid")
    conn = db.connect()
    try:
        conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                     "VALUES ('O1','u1','shipped',899,datetime('now'))")
        conn.commit()
    finally:
        conn.close()
    attribute_once(db=db)
    assert db.get_outreach_draft(did)["outcome"] == "converted"


def test_no_progress_counts_as_no_change(db):
    from app.scripts.attribute_outreach import attribute_once
    did = _sent_draft(db, status_at_send="unpaid")
    conn = db.connect()
    try:
        conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                     "VALUES ('O1','u1','unpaid',899,datetime('now'))")
        conn.commit()
    finally:
        conn.close()
    attribute_once(db=db)
    assert db.get_outreach_draft(did)["outcome"] == "no_change"


def test_backward_status_is_not_converted(db):
    """状态倒退(退款)不算转化——只认向前推进。"""
    from app.scripts.attribute_outreach import attribute_once
    did = _sent_draft(db, status_at_send="shipped")
    conn = db.connect()
    try:
        conn.execute("INSERT INTO orders (order_id,user,status,total,created_at,refund_status) "
                     "VALUES ('O1','u1','refund_processing',899,datetime('now'),'requested')")
        conn.commit()
    finally:
        conn.close()
    attribute_once(db=db)
    assert db.get_outreach_draft(did)["outcome"] == "no_change"


def test_attribution_is_idempotent(db):
    from app.scripts.attribute_outreach import attribute_once
    _sent_draft(db)
    first = attribute_once(db=db)
    second = attribute_once(db=db)
    assert first["checked"] >= 1 and second["checked"] == 0


def test_stats(db):
    from app.scripts.attribute_outreach import attribute_once
    for i in range(3):
        did = _sent_draft(db, oid=f"O{i}")
        if i < 2:
            conn = db.connect()
            try:
                conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                             "VALUES (?,'u1','shipped',899,datetime('now'))", (f"O{i}",))
                conn.commit()
            finally:
                conn.close()
    attribute_once(db=db)
    s = db.outreach_stats(window_days=30)
    assert s["sent"] == 3 and s["converted"] == 2
    assert s["conversion_rate"] == pytest.approx(2 / 3)


def test_stats_empty_is_zero(db):
    s = db.outreach_stats(window_days=7)
    assert s["sent"] == 0 and s["conversion_rate"] == 0.0
