import pytest

from app.db import Database, set_db
from app.db.seed import seed_from_mock


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.init_schema()
    seed_from_mock(d)
    set_db(d)
    return d


def test_query_order_hits_db():
    from app.agent.tools.order import query_order
    r = query_order("ORD-20240115-001")
    assert r["success"] is True
    assert r["order"]["items"][0]["sku"] == "SHOE-270-BK-42"


def test_query_order_missing():
    from app.agent.tools.order import query_order
    r = query_order("NOPE")
    assert r["success"] is False


def test_query_product_by_id():
    from app.agent.tools.product import query_product
    r = query_product("ELEC-APP-002")
    assert r["success"] is True
    assert r["products"][0]["name"] == "Apple AirPods Pro 2"


def test_query_product_by_keyword():
    from app.agent.tools.product import query_product
    r = query_product("运动鞋")
    assert r["success"] is True
    assert any("运动鞋" in p["category"] or "运动鞋" in p["name"] for p in r["products"])


def test_query_logistics():
    from app.agent.tools.logistics import query_logistics
    r = query_logistics("ORD-20240115-001")
    assert r["success"] is True
    assert r["logistics"]["carrier"] == "顺丰速运"


def test_list_user_orders_returns_every_seeded_order():
    """auth 全局关闭时(见 conftest._force_local_session_backends)
    list_user_orders 不做按用户过滤,应当原样覆盖 mock_data 里种下的每一笔订单。

    比对 order_id **集合**而不是数量:数量断言是个魔法数字,每次往 mock_data
    里加订单(如 N4 给"评价"演示补的几笔已签收订单)都得跟着手改这个数字,
    还测不出"是不是那几笔"这个真正关心的行为。比对集合会随 mock_data 自动
    更新期望,这条测试真正验证的性质是"没做用户过滤时,列表 = 全部种子订单"。
    """
    from app.agent.tools.user_orders import list_user_orders
    from app.agent.tools.mock_data import ORDERS
    r = list_user_orders()
    assert r["success"] is True
    got_ids = {o["order_id"] for o in r["orders"]}
    assert got_ids == set(ORDERS.keys())


def test_apply_refund_mutates_db():
    from app.agent.tools.refund import apply_refund
    from app.agent.tools.order import query_order
    from app.agent.consent import consent_scope
    with consent_scope({"refund"}):        # 退款需前置授权
        r = apply_refund("ORD-20240115-001", "尺码不合适")
    assert r["success"] is True
    # 退款后再查订单，状态已变（真实数据流动）
    assert query_order("ORD-20240115-001")["order"]["status"] == "refund_processing"


def test_apply_refund_blocked_without_consent():
    from app.agent.tools.refund import apply_refund
    r = apply_refund("ORD-20240115-001", "尺码不合适")
    assert r["success"] is False and r.get("need_confirm") is True


def test_apply_refund_already_processing():
    from app.agent.tools.refund import apply_refund
    r = apply_refund("ORD-20240118-004", "x")  # 该订单初始即 refund_processing
    assert r["success"] is False
