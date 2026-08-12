"""商品服务连不上 ≠ 商品不存在。

**实测缺陷**(走查商城时抓到)。hmdp 挂掉的那一刻,两个**相邻**端点对同一个故障给出了
两种说法:

    /api/products    → {"products": [], "degraded": true, "reason": "商品服务连不上(ConnectError)"}
    /api/product/1   → {"product": null}                    ← 没有任何降级标记

而 `/api/products` 的 docstring 有一整段专讲这条纪律:

    **降级必须可区分**。改造前失败时 return {"products": []},前端因此无法分辨
    "这家店真的没有商品"和"商品服务连不上"——页面显示"0 件商品",没有报错、没有日志。
    **买家看到一个空店铺,运维看到一切正常。**

12 行之下的 `/api/product/{item_id}` 却写着"失败/无返回 null",把两件事并列得像是理所
当然。而 `_fetch_hmdp_product` 自己的注释也早就点明了后果——"一次代理/网络故障会在界面
上呈现为『这个商品下架了』"——它选的缓解是"失败必须留日志"。**日志不是给买家的出口**:
买家看到的仍然是下架。

最疼的是下单那一处:`create_order` 把同一个 None 翻成 `404 商品不存在或已下架`,
**网络抖一下就告诉一个正要付钱的买家这件商品没了**,而他不会再回来试。
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "")
    monkeypatch.setattr(st.settings, "auth_enabled", False)
    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "ecom.db"))
    db.init_schema()
    set_db(db)
    from app.api.app import create_app
    c = TestClient(create_app())
    yield c
    set_db(None)


def _break_product_service(client, monkeypatch):
    """让 hmdp 商品查询抛连接错误(服务连不上,不是商品不存在)。"""
    import httpx

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): raise httpx.ConnectError("refused")
        def post(self, *a, **kw): raise httpx.ConnectError("refused")

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: C())


# --------------------------------------------------------------------------
# 商品详情
# --------------------------------------------------------------------------

def test_detail_reports_degraded_when_service_is_down(client, monkeypatch):
    """核心断言:服务连不上时必须带 degraded,前端才能说"稍后再试"而不是"下架了"。"""
    _break_product_service(client, monkeypatch)
    d = client.get("/api/product/1").json()
    assert d["product"] is None
    assert d["degraded"] is True
    assert "连不上" in d.get("reason", "")


def test_detail_not_degraded_when_product_really_missing(client, monkeypatch):
    """真的没有这个商品时 degraded=false——两种 null 必须分得开。"""
    import app.api.app as app_mod
    # 非数字 id 走的是"不是合法商品号"这条早退,不发请求也不算故障
    d = client.get("/api/product/not-a-number").json()
    assert d["product"] is None
    assert d["degraded"] is False


def test_detail_ok_path_unchanged(client, monkeypatch):
    import json

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True,
                            "data": {"id": 1, "name": "鞋", "price": 899, "stock": 5}}
            return R()

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: C())
    d = client.get("/api/product/1").json()
    assert d["product"] is not None
    assert d["degraded"] is False


# --------------------------------------------------------------------------
# 下单:最疼的那一处
# --------------------------------------------------------------------------

def test_order_says_try_again_not_delisted(client, monkeypatch):
    """**买家点了「立即购买」之后看到的那句话。**

    把一次网络故障说成"已下架",等于劝退一个正要付钱的人,而且他不会再回来试。
    503(暂时不可用、值得重试)与 404(这件商品没了)对应的买家行为完全不同。
    """
    _break_product_service(client, monkeypatch)
    r = client.post("/api/order", json={"item_id": "1", "quantity": 1})
    assert r.status_code == 503, f"故障被当成了下架: {r.status_code} {r.text[:80]}"
    assert "下架" not in r.text
    assert "稍后再试" in r.text


def test_order_still_404_when_really_missing(client, monkeypatch):
    """真的没有这个商品时仍然 404——这次改动不能把两种情况反过来混。"""
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw):
            class R:
                status_code = 200
                @staticmethod
                def json(): return {"success": False, "data": None}
            return R()

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: C())
    r = client.post("/api/order", json={"item_id": "999", "quantity": 1})
    assert r.status_code == 404


# --------------------------------------------------------------------------
# 购物车:异常不能穿透成 500
# --------------------------------------------------------------------------

def test_cart_survives_product_service_outage(client, monkeypatch):
    """购物车行是买家自己加的,不该因为一次查询失败就从界面上消失,更不该 500。"""
    from app.db import get_db
    get_db().add_to_cart("default", "1", 2)   # auth 关闭时 _resolve_user 回退成 default
    _break_product_service(client, monkeypatch)

    r = client.get("/api/cart")
    assert r.status_code == 200, f"商品服务故障穿透成了 {r.status_code}"
    d = r.json()
    assert len(d["items"]) == 1, "买家自己加的行不该消失"
    assert d["items"][0]["product_missing"] is True
    assert d["degraded"] is True, (
        "满车商品都显示信息缺失而不说明原因,买家会以为自己加的东西全下架了")


def test_cart_not_degraded_normally(client, monkeypatch):
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True,
                            "data": {"id": 1, "name": "鞋", "price": 899, "stock": 5}}
            return R()

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: C())
    from app.db import get_db
    get_db().add_to_cart("default", "1", 2)   # auth 关闭时 _resolve_user 回退成 default
    d = client.get("/api/cart").json()
    assert d["degraded"] is False
    assert d["items"][0]["product_missing"] is False
