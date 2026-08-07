"""跟进序列:到期推进、五条终止条件、一人一链、产物仍是待审草稿。"""

import pytest

from app.db.database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    return d


def _stale(d, fid, hours=72):
    conn = d.connect()
    try:
        conn.execute("UPDATE outreach_followups SET next_touch_at = "
                     "datetime('now','-'||?||' hours') WHERE id = ?", (hours, fid))
        conn.commit()
    finally:
        conn.close()


def test_start_and_due(db):
    fid = db.start_followup("u1", "unpaid_order", "C1", max_steps=3)
    assert db.due_followups() == []          # 刚建的还没到期
    _stale(db, fid)
    assert [f["id"] for f in db.due_followups()] == [fid]


def test_one_active_chain_per_user_and_kind(db):
    """防两条链并行轰炸同一个买家。"""
    assert db.start_followup("u1", "unpaid_order", "C1") is not None
    assert db.start_followup("u1", "unpaid_order", "C2") is None


def test_different_kind_can_coexist(db):
    assert db.start_followup("u1", "unpaid_order", "C1") is not None
    assert db.start_followup("u1", "abandoned_cart", "C2") is not None


def test_advance_increments_and_reschedules(db):
    fid = db.start_followup("u1", "unpaid_order", "C1", max_steps=3)
    _stale(db, fid)
    db.advance_followup(fid)
    row = db.active_followup("u1", "unpaid_order")
    assert row["step"] == 2
    assert db.due_followups() == []           # 已重排到未来


def test_reaching_max_steps_finishes(db):
    fid = db.start_followup("u1", "unpaid_order", "C1", max_steps=2)
    for _ in range(2):
        _stale(db, fid)
        db.advance_followup(fid)
    assert db.active_followup("u1", "unpaid_order") is None


def test_stop_on_arbitration_refusal(db, monkeypatch):
    """人工接管中一律停链——复用仲裁,不新建判断。"""
    from app.multi_agent import followup
    monkeypatch.setattr(followup, "check_outreach_allowed",
                        lambda uid, hitl=None, db=None: (False, "manual_takeover", "接管中"))
    fid = db.start_followup("u1", "unpaid_order", "C1")
    _stale(db, fid)
    out = followup.run_due(db=db)
    assert out["stopped"] == 1
    assert db.active_followup("u1", "unpaid_order") is None


def test_stop_when_opportunity_gone(db, monkeypatch):
    from app.multi_agent import followup
    monkeypatch.setattr(followup, "check_outreach_allowed",
                        lambda uid, hitl=None, db=None: (True, "", ""))
    monkeypatch.setattr(followup, "_opportunity_still_open",
                        lambda row, db: False)
    fid = db.start_followup("u1", "unpaid_order", "C1")
    _stale(db, fid)
    out = followup.run_due(db=db)
    assert out["stopped"] == 1


def test_shipped_no_care_stops_once_delivered(db):
    """物流播报不能在包裹签收之后还反复提——一旦该买家名下已无 status='shipped'
    的订单(已推进到 delivered),商机就该判定为已消失。"""
    from app.multi_agent.followup import _opportunity_still_open

    conn = db.connect()
    conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                 "VALUES ('O1','u1','shipped',199,datetime('now'))")
    conn.commit()
    conn.close()
    row = {"kind": "shipped_no_care", "user_id": "u1"}
    assert _opportunity_still_open(row, db) is True

    conn = db.connect()
    conn.execute("UPDATE orders SET status = 'delivered' WHERE order_id = 'O1'")
    conn.commit()
    conn.close()
    assert _opportunity_still_open(row, db) is False


def test_delivered_no_review_stops_once_reviewed(db):
    """评价邀约不能在买家评完价之后还继续提——复用 reviewable_items,
    买家一旦提交评价,该商机必须判定为已消失。"""
    from app.multi_agent.followup import _opportunity_still_open

    conn = db.connect()
    conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                 "VALUES ('O1','u1','delivered',199,datetime('now'))")
    conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                 "VALUES ('O1','跑鞋','P001',1,199)")
    conn.commit()
    conn.close()
    row = {"kind": "delivered_no_review", "user_id": "u1"}
    assert _opportunity_still_open(row, db) is True

    assert db.create_review(order_id="O1", user_id="u1", sku="P001",
                            rating=5, content="好") is not None
    assert _opportunity_still_open(row, db) is False


def test_advance_produces_a_draft_not_a_send(db, monkeypatch):
    """"持续沟通"= 序列自动推进,不是自动发送;每一步仍需人工批准。"""
    from app.multi_agent import followup
    monkeypatch.setattr(followup, "check_outreach_allowed",
                        lambda uid, hitl=None, db=None: (True, "", ""))
    monkeypatch.setattr(followup, "_opportunity_still_open", lambda row, db: True)
    monkeypatch.setattr(followup, "_compose_followup_text",
                        lambda row, db: "第二次提醒您这单还没付款")
    fid = db.start_followup("u1", "unpaid_order", "C1")
    _stale(db, fid)
    followup.run_due(db=db)
    drafts = db.list_outreach_drafts(status="draft")
    assert len(drafts) == 1 and drafts[0]["status"] == "draft"
    assert db.list_outreach_drafts(status="sent") == []
