"""下单时下游挂了,不能给买家一个裸 500。

**实测缺陷**(前端体验时踩到)。链条是:

    Redis 挂掉(ConnectionRefused)
      → hmdp 只读正常(GET /product/1 → 200 / 413ms)
      → hmdp 写入路径 /order 挂住(它要 Redis 做库存)
      → 我们的端点不捕获 httpx.ReadTimeout
      → 买家点「去下单」看到 500 Internal Server Error

商城看着一切正常(商品、价格、库存都在),**只有下单会炸**——而买家不会知道
问题出在别处,他只会觉得这家店坏了。

**超时与连接失败必须分开,因为"订单到底建没建"的答案不一样**:

- `TimeoutException` = 我们不知道对面做了什么。请求可能已到达并建了单。
  所以**不释放幂等键**(释放了,同一个 key 重试会建出第二笔)、**不核销议价**,
  文案让买家**去看订单**而不是"请重试"——劝一个可能已经下过单的人再下一次,
  是这里最坏的建议。
- `RequestError`(拒绝/DNS/断开)= 请求**没送达**,这一点是确定的。
  可以安全地释放幂等键让他重试。

这条区分是本文件的全部要点;两个方向各有断言,少一边都会退化成"反正都报 503"。
"""

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "")
    monkeypatch.setattr(st.settings, "auth_enabled", False)
    # demo 模式让 default 用户拿到 hmdp token → 走 hmdp 建单分支
    monkeypatch.setattr(st.settings, "demo_mode", True)
    monkeypatch.setattr(st.settings, "demo_hmdp_user_id", "default")
    monkeypatch.setattr(st.settings, "demo_hmdp_token", "tok")
    import app.db as db_mod
    from app.db import Database

    db = Database(db_path=str(tmp_path / "ecom.db"))
    db.init_schema()
    monkeypatch.setattr(db_mod, "_DB", db)

    from app.api.app import create_app
    return TestClient(create_app()), db


def _hmdp(monkeypatch, post_behaviour):
    """商品查询正常(复现现场:只读活着),只有下单 POST 出问题。"""
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "data": {"id": 1, "title": "鞋",
                                                      "price": 89900, "stock": 9}}
            return R()
        def post(self, *a, **kw): return post_behaviour()

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: C())


def _order(c, key=None):
    return c.post("/api/order", json={"item_id": "1", "quantity": 1},
                  headers={"Idempotency-Key": key} if key else {})


# --------------------------------------------------------------------------
# 超时:结果未知
# --------------------------------------------------------------------------

def test_timeout_is_503_not_500(client, monkeypatch):
    """**核心断言。** 修复前这里是裸 500 Internal Server Error。"""
    c, _ = client
    _hmdp(monkeypatch, lambda: (_ for _ in ()).throw(httpx.ReadTimeout("timed out")))
    r = _order(c)
    assert r.status_code == 503, f"下游超时穿透成了 {r.status_code}"


def test_timeout_tells_the_buyer_to_check_orders(client, monkeypatch):
    """文案要让买家去看订单,**不能劝他重试**——他可能已经下过单了。"""
    c, _ = client
    _hmdp(monkeypatch, lambda: (_ for _ in ()).throw(httpx.ReadTimeout("timed out")))
    body = _order(c).text
    assert "我的订单" in body
    assert "不要立即重试" in body


def test_timeout_keeps_the_idempotency_key(client, monkeypatch):
    """超时**不释放**幂等键:释放了,同一个 key 重试就会建出第二笔订单。"""
    c, db = client
    _hmdp(monkeypatch, lambda: (_ for _ in ()).throw(httpx.ReadTimeout("timed out")))
    assert _order(c, key="k1").status_code == 503
    # 键仍被占着 → 再来同一个 key 只会拿到"正在处理中",不会重新建单
    assert db.claim_idempotency_key("default", "k1")[0] == "in_flight"


def test_timeout_does_not_consume_the_bargain_deal(client, monkeypatch):
    """那笔单是否存在还不确定,议价成交价不能就这么被吃掉。"""
    c, db = client
    conn = db.connect()
    conn.execute("INSERT INTO products (product_id, name, price, stock, floor_price) "
                 "VALUES (?,?,?,?,?)", ("SKU-1", "鞋", 899.0, 9, 700.0))
    conn.commit()
    conn.close()
    db.record_bargain_deal("default", "SKU-1", 780.0)

    _hmdp(monkeypatch, lambda: (_ for _ in ()).throw(httpx.ReadTimeout("timed out")))
    _order(c)
    assert db.active_bargain_deal("default", "SKU-1") is not None, "超时把成交价吃掉了"


# --------------------------------------------------------------------------
# 连接失败:确定没送达
# --------------------------------------------------------------------------

def test_connect_error_is_503_and_retryable(client, monkeypatch):
    """连接失败 = 请求没送达,这一点确定,所以可以直接说"稍后再试"。"""
    c, _ = client
    _hmdp(monkeypatch, lambda: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    r = _order(c)
    assert r.status_code == 503
    assert "稍后再试" in r.text
    assert "我的订单" not in r.text, "没送达却让买家去查订单,是在制造困惑"


def test_connect_error_releases_the_key(client, monkeypatch):
    """**与超时相反**:确定没送达,就该让买家用同一个 key 重试。"""
    c, db = client
    _hmdp(monkeypatch, lambda: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    assert _order(c, key="k2").status_code == 503
    assert db.claim_idempotency_key("default", "k2")[0] == "claimed", \
        "连接失败后键没释放,买家永远卡在'正在处理中'"


# --------------------------------------------------------------------------
# 正常路径不受影响
# --------------------------------------------------------------------------

def test_success_path_unchanged(client, monkeypatch):
    class R:
        status_code = 200
        @staticmethod
        def json(): return {"success": True, "data": "HMDP-1"}

    c, _ = client
    _hmdp(monkeypatch, lambda: R())
    r = _order(c)
    assert r.status_code == 200 and r.json()["order_id"] == "HMDP-1"


def test_hmdp_business_failure_still_502(client, monkeypatch):
    """hmdp 明确说失败(库存不足等)仍是 502,不能被这次改动吞进 503。"""
    class R:
        status_code = 200
        @staticmethod
        def json(): return {"success": False, "errorMsg": "库存不足"}

    c, _ = client
    _hmdp(monkeypatch, lambda: R())
    r = _order(c)
    assert r.status_code == 502 and "库存不足" in r.text
