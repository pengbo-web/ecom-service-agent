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


def test_list_orders_returns_every_seeded_order(db):
    """`list_orders()` 必须原样覆盖 mock_data 里种下的每一笔订单——不多不少。

    直接比对 order_id **集合**,而不是数量:数量断言是个魔法数字,每次往
    mock_data 里加一笔订单(如 N4 给"评价"演示补的几笔已签收订单)都得跟着手改
    这个数字,而且改错了(比如手滑加错/漏加)测试也照样能过,因为它只关心
    "个数对不对",不关心"是不是那几笔"。比对集合则会随 mock_data 自动更新
    期望,还能真正验证返回的是"种了什么就原样能读回什么"这个行为本身。
    """
    from app.agent.tools.mock_data import ORDERS
    got_ids = {o["order_id"] for o in db.list_orders()}
    assert got_ids == set(ORDERS.keys())


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
