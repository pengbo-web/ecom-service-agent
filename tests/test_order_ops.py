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
