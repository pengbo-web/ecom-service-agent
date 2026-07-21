import pytest

from app.db import Database, set_db
from app.db.seed import seed_from_mock
from app.agent.consent import consent_scope
from app.agent.tools.refund import apply_refund
from app.agent.tools.order import query_order


@pytest.fixture(autouse=True)
def _db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.init_schema()
    seed_from_mock(d)
    set_db(d)
    return d


def test_refund_blocked_without_consent():
    r = apply_refund("ORD-20240115-001", "尺码不合适")
    assert r["success"] is False and r.get("need_confirm") is True
    # 未授权:订单状态未变
    assert query_order("ORD-20240115-001")["order"]["status"] == "shipped"


def test_refund_executes_with_consent():
    with consent_scope({"refund"}):
        r = apply_refund("ORD-20240115-001", "尺码不合适")
    assert r["success"] is True
    assert query_order("ORD-20240115-001")["order"]["status"] == "refund_processing"


def test_bargain_accept_needs_consent():
    from app.config.settings import settings
    if not settings.bargain_enabled:
        pytest.skip("bargain 未启用")
    from app.agent.tools.bargain import negotiate_price
    # 买家出价 >= 标价 → decision=accept,应被拦
    prod = query_order  # noqa: 占位,下面用真实商品
    from app.agent.tools.product import query_product
    p = query_product("ELEC-APP-002")["products"][0]
    high = p["price"] + 100
    r = negotiate_price("ELEC-APP-002", buyer_offer=high)
    assert r.get("need_confirm") is True and r["action"] == "deal_close"
    with consent_scope({"deal_close"}):
        r2 = negotiate_price("ELEC-APP-002", buyer_offer=high)
    assert r2["success"] is True and r2["decision"] == "accept"
