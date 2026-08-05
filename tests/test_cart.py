"""购物车:加购/去重/弃单口径,以及它不承担结算。"""

import pytest

from app.db.database import Database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    from app.agent.tools import cart as c
    monkeypatch.setattr(c, "get_db", lambda: d)
    return d


def test_add_and_list(db):
    db.add_to_cart("u1", "P001", 2)
    items = db.list_cart("u1")
    assert len(items) == 1 and items[0]["quantity"] == 2


def test_add_same_sku_accumulates_not_duplicates(db):
    db.add_to_cart("u1", "P001", 1)
    db.add_to_cart("u1", "P001", 2)
    items = db.list_cart("u1")
    assert len(items) == 1 and items[0]["quantity"] == 3


def test_remove(db):
    db.add_to_cart("u1", "P001", 1)
    assert db.remove_from_cart("u1", "P001") is True
    assert db.list_cart("u1") == []


def test_converted_cart_leaves_active_list(db):
    db.add_to_cart("u1", "P001", 1)
    db.mark_cart_converted("u1", ["P001"])
    assert db.list_cart("u1") == []


def test_abandoned_needs_to_be_stale(db):
    db.add_to_cart("u1", "P001", 1)                 # 刚加的不算弃单
    assert db.abandoned_carts(hours=48) == []


def test_abandoned_after_threshold(db):
    conn = db.connect()
    try:
        conn.execute("INSERT INTO carts (user_id,sku,quantity,added_at,status) "
                     "VALUES ('u1','P001',1,datetime('now','-72 hours'),'active')")
        conn.commit()
    finally:
        conn.close()
    assert [c["sku"] for c in db.abandoned_carts(hours=48)] == ["P001"]


def test_cart_tools_are_buyer_side_and_have_no_checkout(db):
    """购物车不承担结算——与"不代客下单"同一条底线。"""
    from app.agent.tools import cart
    assert not hasattr(cart, "checkout")
    assert not hasattr(cart, "place_order")


# ---------------------------------------------------------------------------
# set_cart_quantity:改数量走"设置"而非"累加",与 add_to_cart 互补
# ---------------------------------------------------------------------------

def test_set_quantity_increases(db):
    db.add_to_cart("u1", "P001", 1)
    assert db.set_cart_quantity("u1", "P001", 5) is True
    assert db.list_cart("u1")[0]["quantity"] == 5


def test_set_quantity_decreases(db):
    db.add_to_cart("u1", "P001", 5)
    assert db.set_cart_quantity("u1", "P001", 2) is True
    assert db.list_cart("u1")[0]["quantity"] == 2


def test_set_quantity_rejects_non_positive(db):
    """非正数直接拒绝,不做静默删除——"设为 0"与"移除"是两件事。"""
    db.add_to_cart("u1", "P001", 3)
    assert db.set_cart_quantity("u1", "P001", 0) is False
    assert db.set_cart_quantity("u1", "P001", -1) is False
    assert db.list_cart("u1")[0]["quantity"] == 3   # 未被非法值改动


def test_set_quantity_does_not_touch_other_users_cart(db):
    """越权改别人购物车里同名 sku 的数量必须不生效。"""
    db.add_to_cart("u1", "P001", 2)
    db.add_to_cart("u2", "P001", 9)
    assert db.set_cart_quantity("u1", "P001", 4) is True
    assert db.list_cart("u1")[0]["quantity"] == 4
    assert db.list_cart("u2")[0]["quantity"] == 9   # u2 的行完全不受影响


def test_set_quantity_missing_sku_is_not_created(db):
    """sku 不在购物车里不是"设置成功"的一种,不会隐式创建一行。"""
    assert db.set_cart_quantity("u1", "P404", 3) is False
    assert db.list_cart("u1") == []
