import pytest

from app.db import Database
from app.db.seed import seed_from_mock


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.init_schema()
    seed_from_mock(d)
    return d


def test_get_order_shape(db):
    order = db.get_order("ORD-20240115-001")
    assert order["order_id"] == "ORD-20240115-001"
    assert order["status"] == "shipped"
    assert order["tracking_number"] == "SF1234567890"
    assert isinstance(order["items"], list) and len(order["items"]) == 1
    assert order["items"][0]["sku"] == "SHOE-270-BK-42"


def test_get_order_missing_returns_none(db):
    assert db.get_order("NOPE") is None


def test_list_orders_count(db):
    from app.agent.tools.mock_data import ORDERS
    assert len(db.list_orders()) == len(ORDERS)


def test_get_product_specs_is_dict(db):
    p = db.get_product("ELEC-APP-002")
    assert p["name"] == "Apple AirPods Pro 2"
    assert isinstance(p["specs"], dict)
    assert p["specs"]["颜色"] == "白色"


def test_all_products_count(db):
    from app.agent.tools.mock_data import PRODUCTS
    all_p = db.all_products()
    assert len(all_p) == len(PRODUCTS)
    assert all(isinstance(p["specs"], dict) for p in all_p)


def test_get_logistics_shape(db):
    lg = db.get_logistics("SF1234567890")
    assert lg["carrier"] == "顺丰速运"
    assert lg["status"] == "in_transit"
    assert len(lg["events"]) == 4
    assert lg["events"][0]["description"] == "快件已揽收"


def test_get_logistics_missing_returns_none(db):
    assert db.get_logistics("NOPE") is None
