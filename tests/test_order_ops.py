"""B2:新增高频客服工具——改地址/取消/催发货/开发票/优惠券。风险动作接 consent 门。"""

import pytest

from app.db.database import Database
from app.db import set_db
from app.agent.consent import consent_scope
from app.agent.tools.order_ops import (
    change_address, cancel_order, expedite_shipping, issue_invoice, query_coupons,
)


@pytest.fixture
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    conn = d.connect()
    conn.executemany(
        "INSERT INTO orders (order_id, user, status, total, created_at) VALUES (?, ?, ?, ?, ?)",
        [
            ("ORD-P", "小明", "pending", 100.0, "2024-01-01"),
            ("ORD-S", "小红", "shipped", 200.0, "2024-01-02"),
        ],
    )
    conn.execute(
        "INSERT INTO order_items (order_id, name, sku, quantity, price) VALUES (?, ?, ?, ?, ?)",
        ("ORD-S", "运动鞋", "SKU-1", 1, 200.0),
    )
    conn.commit()
    conn.close()
    set_db(d)
    return d


# ---- change_address ----
def test_change_address_gated_without_consent(db):
    r = change_address("ORD-P", "北京市朝阳区xx路1号")
    assert r["success"] is False and r["need_confirm"] is True and r["action"] == "change_address"
    assert db.get_order("ORD-P")["shipping_address"] is None   # 未授权 → 未改

def test_change_address_executes_with_consent(db):
    with consent_scope({"change_address"}):
        r = change_address("ORD-P", "北京市朝阳区xx路1号")
    assert r["success"] is True
    assert db.get_order("ORD-P")["shipping_address"] == "北京市朝阳区xx路1号"

def test_change_address_blocked_after_shipped(db):
    with consent_scope({"change_address"}):
        r = change_address("ORD-S", "新地址")
    assert r["success"] is False and "发货" in r["error"]


# ---- cancel_order ----
def test_cancel_gated_without_consent(db):
    r = cancel_order("ORD-P")
    assert r["success"] is False and r["need_confirm"] is True and r["action"] == "cancel_order"
    assert db.get_order("ORD-P")["status"] == "pending"        # 未授权 → 未取消

def test_cancel_executes_with_consent(db):
    with consent_scope({"cancel_order"}):
        r = cancel_order("ORD-P")
    assert r["success"] is True
    assert db.get_order("ORD-P")["status"] == "cancelled"

def test_cancel_blocked_after_shipped(db):
    with consent_scope({"cancel_order"}):
        r = cancel_order("ORD-S")
    assert r["success"] is False


# ---- expedite / invoice / coupons(非风险)----
def test_expedite_pending_and_shipped(db):
    assert expedite_shipping("ORD-P")["success"] is True
    assert expedite_shipping("ORD-S")["success"] is True
    assert expedite_shipping("ORD-X")["success"] is False      # 不存在

def test_issue_invoice_ok_for_shipped(db):
    r = issue_invoice("ORD-S", title="某某公司", tax_id="91xxx")
    assert r["success"] is True
    assert r["invoice"]["amount"] == 200.0
    assert r["invoice"]["title"] == "某某公司"
    assert r["invoice"]["items"][0]["name"] == "运动鞋"

def test_issue_invoice_blocked_for_pending(db):
    assert issue_invoice("ORD-P")["success"] is False          # 未支付不可开票

def test_query_coupons(db):
    r = query_coupons()
    assert r["success"] is True and len(r["coupons"]) >= 1
    assert all("code" in c and "discount" in c for c in r["coupons"])


# ---- 回归:取消后不得二次退款(双重退款防线)----
def test_refund_blocked_after_cancel(db):
    from app.agent.tools.refund import apply_refund
    with consent_scope({"cancel_order"}):
        assert cancel_order("ORD-P")["success"] is True
    assert db.get_order("ORD-P")["status"] == "cancelled"
    # 已取消订单再退款:即便已授权也应被拦(cancel 已承诺原路退回),防二次退款
    with consent_scope({"refund"}):
        r = apply_refund("ORD-P", "不想要了")
    assert r["success"] is False
    assert db.get_order("ORD-P")["status"] == "cancelled"   # 未被改成 refund_processing


# ---- 回归:空新地址不得覆盖收货地址 ----
def test_change_address_rejects_empty(db):
    with consent_scope({"change_address"}):
        r = change_address("ORD-P", "   ")
    assert r["success"] is False
    assert db.get_order("ORD-P")["shipping_address"] is None


# ---- 回归:退款中订单不得开具全额发票 ----
def test_issue_invoice_blocked_for_refunding(db):
    from app.agent.tools.refund import apply_refund
    with consent_scope({"refund"}):
        assert apply_refund("ORD-S", "尺码不合")["success"] is True
    assert db.get_order("ORD-S")["status"] == "refund_processing"
    r = issue_invoice("ORD-S", title="某公司")
    assert r["success"] is False   # 退款流程中禁开票,防金额/凭证不一致
