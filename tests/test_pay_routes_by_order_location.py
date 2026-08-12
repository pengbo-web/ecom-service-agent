"""支付要按"这笔订单在哪"路由,不按"这个用户有没有 token"。

**实测缺陷**(走查订单全流程时抓到)。给 user='1'(demo 模式下有 hmdp token)在**本地
库**建一笔 unpaid 订单,点支付:

    POST /api/order/{id}/pay → 502 {"detail":"支付服务暂时不可用，请稍后再试"}

订单明明在本地库,支付却去找 hmdp 了。hmdp 活着时更隐蔽——会拿到"订单不存在、不属于
当前用户,或已完成支付"。**这笔单买家永远付不了款。**

而这个端点的 docstring 记录的正是他们**修过的反向问题**:

> 改造前这里只改本地库,而 `POST /api/order` 对 demo 用户是把订单建到 hmdp 的…于是
> 买家看到自己刚下的单、点「去支付」,拿到的却是「订单不存在…」:订单在 hmdp 里,
> 本地库根本没有这一行。

当时的修法是"支付跟着下单的路由规则走"。但那条规则按**用户**判,隐含假设"有 token 的
买家的订单一定在 hmdp"。**这个假设不成立**:本地库里确实存着 user='1' 的订单(实测三笔),
且 `unpaid_flow_enabled=True` 时本地单起始状态就是 unpaid。于是同一个 bug 被镜像了一次。

按订单位置判从根上避免这一类:它不依赖"谁有 token",所以不会因为路由规则再改一次而
重新失配。
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "")
    monkeypatch.setattr(st.settings, "auth_enabled", False)
    # demo 模式:让 _hmdp_token_for_user("default") 返回一个 token,复现"有 token"的买家
    monkeypatch.setattr(st.settings, "demo_mode", True)
    monkeypatch.setattr(st.settings, "demo_hmdp_user_id", "default")
    monkeypatch.setattr(st.settings, "demo_hmdp_token", "tok-demo")

    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "ecom.db"))
    db.init_schema()
    set_db(db)
    from app.api.app import create_app
    c = TestClient(create_app())
    yield c
    set_db(None)


def _local_unpaid_order(user="default"):
    from app.db import get_db
    return get_db().create_order(
        user=user, items=[{"name": "鞋", "sku": "S1", "quantity": 1, "price": 199.0}],
        total=199.0, status="unpaid", shipping_address="示例路 1 号")["order_id"]


def _hmdp_would_fail(monkeypatch):
    """任何走 hmdp 的支付都会失败——用它证明"根本没去 hmdp"。"""
    import httpx

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **kw): raise httpx.ConnectError("refused")
        def get(self, *a, **kw): raise httpx.ConnectError("refused")

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: C())


# --------------------------------------------------------------------------
# 核心:本地订单必须走本地支付
# --------------------------------------------------------------------------

def test_local_order_is_payable_even_when_user_has_hmdp_token(client, monkeypatch):
    """核心断言:订单在本地就本地付,不看用户有没有 token。

    修复前这里是 502(hmdp 连不上)或 400(hmdp 说没这个单)——买家永远付不了款。
    """
    oid = _local_unpaid_order()
    _hmdp_would_fail(monkeypatch)   # 只要去了 hmdp 就会失败

    r = client.post(f"/api/order/{oid}/pay")
    assert r.status_code == 200, f"本地订单没走本地支付: {r.status_code} {r.text[:120]}"
    d = r.json()
    assert d["success"] is True
    assert d["status"] == "pending", "unpaid → pending 没生效"


def test_paying_twice_is_idempotent(client, monkeypatch):
    """幂等仍由 pay_order 的条件更新兜住:第二次拿 400,不改变状态。"""
    oid = _local_unpaid_order()
    _hmdp_would_fail(monkeypatch)
    assert client.post(f"/api/order/{oid}/pay").status_code == 200
    assert client.post(f"/api/order/{oid}/pay").status_code == 400


def test_cannot_pay_someone_elses_local_order(client, monkeypatch):
    """归属校验不能因为改了路由就丢:付别人的单必须失败。"""
    oid = _local_unpaid_order(user="someone_else")
    _hmdp_would_fail(monkeypatch)
    r = client.post(f"/api/order/{oid}/pay")
    assert r.status_code == 400
    from app.db import get_db
    assert get_db().get_order(oid)["status"] == "unpaid", "别人的单被改了状态"


# --------------------------------------------------------------------------
# 反向:不在本地的订单仍然走 hmdp
# --------------------------------------------------------------------------

def test_unknown_order_still_goes_to_hmdp(client, monkeypatch):
    """本地没有这一行时仍要去 hmdp——这次改动不能把 hmdp 那条路掐掉。

    (那正是这个端点当初要修的问题:订单在 hmdp、支付只看本地。)
    """
    calls = []

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, *a, **kw):
            calls.append(url)
            class R:
                status_code = 200
                @staticmethod
                def json(): return {"success": True}
            return R()
        def get(self, *a, **kw):
            class R:
                status_code = 200
                @staticmethod
                def json(): return {"success": True, "data": {"status": 2}}
            return R()

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: C())

    r = client.post("/api/order/HMDP-ONLY-1/pay")
    assert r.status_code == 200
    assert calls, "本地找不到的订单没有去 hmdp——那条路被掐掉了"
    assert "HMDP-ONLY-1" in calls[0]


def test_no_token_and_no_local_order_still_400(client, monkeypatch):
    """既没 token 又不在本地:仍是那句明确的失败,不该变成 500。"""
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "demo_mode", False)   # 没有 token
    r = client.post("/api/order/GHOST-1/pay")
    assert r.status_code == 400
    assert "订单不存在" in r.text
