import pytest

from app.db import Database
from app.db.seed import seed_from_mock


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.init_schema()
    seed_from_mock(d)
    return d


def test_set_refund_mutates_order(db):
    ok = db.set_refund("ORD-20240115-001", "尺码不合适")
    assert ok is True
    order = db.get_order("ORD-20240115-001")
    assert order["status"] == "refund_processing"
    assert order["refund_status"] == "审核中"
    assert order["refund_reason"] == "尺码不合适"
    assert order["refund_requested_at"]  # 非空


def test_set_refund_missing_returns_false(db):
    assert db.set_refund("NOPE", "x") is False


def test_update_order_status(db):
    assert db.update_order_status("ORD-20240120-002", "shipped") is True
    assert db.get_order("ORD-20240120-002")["status"] == "shipped"


def test_update_stock(db):
    assert db.update_stock("ELEC-APP-002", 5) is True
    assert db.get_product("ELEC-APP-002")["stock"] == 5


def test_update_stock_missing_returns_false(db):
    assert db.update_stock("NOPE", 1) is False
