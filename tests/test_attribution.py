"""触达归因:发送时记基线,到期判定是否推进,统计可衡量。"""

import pytest
from fastapi.testclient import TestClient

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


# ---------------------------------------------------------------------------
# review finding 3:归因基线必须只在消息真的送达时才写。上面所有测试只单测
# Database 的方法,从未真正跑过 `approve_draft` 这个唯一会调用
# `set_outreach_baseline` 的地方——一次把基线写挪到投递检查之前这种改动,
# 上面的测试全部保持绿色也不会察觉。这里用真实端点(TestClient,做法与
# tests/test_growth_api.py 一致)钉住:仲裁拒绝、投递失败退回,这两条在
# baseline 写入之前就 return 的路径必须不留基线;投递成功那条路径必须留。
# ---------------------------------------------------------------------------

AUTH = {"X-Admin-Token": "T"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")

    from app.db import Database as _Database, set_db
    d = _Database(db_path=str(tmp_path / "attribution_api_test.db"))
    d.init_schema()
    set_db(d)

    from app.api.app import create_app
    c = TestClient(create_app())
    yield c
    set_db(None)


@pytest.fixture()
def draft(client):
    from app.db import get_db
    return get_db().create_outreach_draft(
        "stale_pending_order", "u1", "O1", "这单还差一步", {}, "未付款", "C1", "growth")


def test_approve_writes_baseline_when_delivery_succeeds(client, draft, monkeypatch):
    """delivery succeeded → baseline written。"""
    from app.db import get_db
    d = get_db()
    conn = d.connect()
    try:
        conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                     "VALUES ('O1','u1','unpaid',899,datetime('now'))")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda dr: True)
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["sent"] is True

    row = d.get_outreach_draft(draft)
    assert row["status"] == "sent"
    assert row["status_at_send"] == "unpaid"   # 基线确实写下了送达那一刻的订单状态


def test_approve_writes_no_baseline_when_delivery_fails_and_reverted(client, draft, monkeypatch):
    """delivery failed and draft reverted → no baseline。"""
    from app.db import get_db
    d = get_db()
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda dr: False)
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False and body["sent"] is False

    row = d.get_outreach_draft(draft)
    assert row["status"] == "draft"        # 退回待审,可重试
    assert row["status_at_send"] is None   # 从未真正送达,不留基线


def test_approve_writes_no_baseline_when_arbitration_refuses(client, draft, monkeypatch):
    """arbitration refused → no baseline。"""
    from app.db import get_db
    from app.multi_agent import arbitration as arb
    d = get_db()

    sent = []
    monkeypatch.setattr(client.app.state, "deliver_outreach",
                        lambda dr: sent.append(1) or True)
    monkeypatch.setattr(
        "app.multi_agent.arbitration.check_outreach_allowed",
        lambda user_id, hitl=None, db=None:
            (False, arb.BLOCK_MANUAL, "该买家的会话正由人工客服接管中"))

    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False
    assert sent == []   # 仲裁拒绝发生在认领之前,根本没投递

    row = d.get_outreach_draft(draft)
    assert row["status"] == "draft"        # 状态没被消耗,还能再批
    assert row["status_at_send"] is None   # 从未真正送达,不留基线
