"""真实未支付态:开关、状态机、归属校验、两个新商机口径。"""

import pytest

from app.db.database import Database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    from app.agent.tools import growth as g
    monkeypatch.setattr(g, "get_db", lambda: d)
    return d


def test_status_label_single_source():
    """待支付的中文名只能有一处定义。"""
    from app.agent.tools.user_orders import STATUS_LABELS
    assert STATUS_LABELS["unpaid"] == "待支付"


def test_pay_moves_unpaid_to_pending(db):
    o = db.create_order("u1", [{"name": "跑鞋", "sku": "P001", "quantity": 1, "price": 899}],
                        899.0, status="unpaid")
    assert db.pay_order(o["order_id"], "u1") is True
    assert db.get_order(o["order_id"])["status"] == "pending"


def test_pay_is_idempotent(db):
    o = db.create_order("u1", [{"name": "跑鞋", "sku": "P001", "quantity": 1, "price": 899}],
                        899.0, status="unpaid")
    assert db.pay_order(o["order_id"], "u1") is True
    assert db.pay_order(o["order_id"], "u1") is False     # 第二次不再生效


def test_pay_rejects_other_users_order(db):
    """越权支付别人的订单必须失败。"""
    o = db.create_order("u1", [{"name": "跑鞋", "sku": "P001", "quantity": 1, "price": 899}],
                        899.0, status="unpaid")
    assert db.pay_order(o["order_id"], "attacker") is False
    assert db.get_order(o["order_id"])["status"] == "unpaid"


def test_unpaid_opportunity_needs_staleness(db):
    from app.agent.tools.growth import find_opportunities
    db.create_order("u1", [{"name": "跑鞋", "sku": "P001", "quantity": 1, "price": 899}],
                    899.0, status="unpaid")
    out = find_opportunities(kind="unpaid_order", window_days=14)
    assert out["success"] is True
    assert out["opportunities"] == []          # 刚下单,还没到催付款的时候


def test_unpaid_opportunity_after_threshold(db):
    from app.agent.tools.growth import find_opportunities
    conn = db.connect()
    try:
        conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                     "VALUES ('O1','u1','unpaid',899,datetime('now','-48 hours'))")
        conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                     "VALUES ('O1','跑鞋','P001',1,899)")
        conn.commit()
    finally:
        conn.close()
    out = find_opportunities(kind="unpaid_order", window_days=14)
    assert [o["order_id"] for o in out["opportunities"]] == ["O1"]
    assert out["opportunities"][0]["situation_label"] == "下单未支付"


def test_abandoned_cart_opportunity(db):
    from app.agent.tools.growth import find_opportunities
    conn = db.connect()
    try:
        conn.execute("INSERT INTO carts (user_id,sku,quantity,added_at,status) "
                     "VALUES ('u1','P001',1,datetime('now','-72 hours'),'active')")
        conn.commit()
    finally:
        conn.close()
    out = find_opportunities(kind="abandoned_cart", window_days=14)
    assert out["success"] is True
    assert out["opportunities"][0]["situation_label"] == "加购未下单"


def test_stale_pending_now_means_paid_but_unshipped(db):
    """旧 kind 语义收窄:它现在专指已付款但久未发货,不再兼指未支付。"""
    from app.agent.tools.growth import OPPORTUNITY_KINDS
    assert "已付款" in OPPORTUNITY_KINDS["stale_pending_order"] or \
           "发货" in OPPORTUNITY_KINDS["stale_pending_order"]


def test_switch_off_restores_current_behaviour(monkeypatch, db):
    """开关关掉时下单直接进 pending,与改造前完全一致。"""
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "unpaid_flow_enabled", False)
    from app.api.app import initial_order_status
    assert initial_order_status() == "pending"
    monkeypatch.setattr(st.settings, "unpaid_flow_enabled", True)
    assert initial_order_status() == "unpaid"
