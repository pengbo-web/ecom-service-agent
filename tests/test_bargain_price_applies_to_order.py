"""议价谈成的价格必须真的作用到订单。

**实测缺陷**(走查议价时抓到,一直挂在遗留清单上)。买家被客服一路谈到 ¥750(标价
¥899),点「立即购买」被收 **¥899**——客服刚亲口答应过的价格。

根因比"下单不查议价"更靠前:**成交这件事根本没落库**。
`bargain_sessions` 只存谈判过程(`rounds` / `last_offer`),accept 分支拿到确认后
也只是 `bump_bargain_state`;而 `app/api/app.py` 里 `bargain` 出现 **0 次**,
下单一律 `total = p["price"] * qty`。

这条比"平台承担运费"更硬:那句是费用归属,这句是**买家实际付多少**。而且它当时的
形态特别糟——议价功能看起来是完整的(阶梯、底价、轮次、落库都有),只有最后一跳断了,
靠 `price_effect` 里一句"需客服核价"把问题遮着。

修法是一条完整的链:`bargain_deals` 表(按 **user + sku**,因为兑现发生在下单请求里,
那里没有 session_id)→ accept + 确认时落一笔 → 下单时取价 → 建单成功后核销(一次性)。

**兑现时重新校验,不信任存下来的数字**:不低于当前底价(底价可能在成交后被调过)、
不高于当前标价(标价降下来时按标价收——绝不能因为"谈过价"反而收得更贵)。
"""

import pytest
from fastapi.testclient import TestClient


LIST_PRICE, FLOOR, DEAL = 899.0, 750.0, 780.0
SKU = "SHOE-270-BK-42"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "")
    monkeypatch.setattr(st.settings, "auth_enabled", False)
    monkeypatch.setattr(st.settings, "demo_mode", False)      # 走本地建单
    import app.db as db_mod
    from app.db import Database

    db = Database(db_path=str(tmp_path / "ecom.db"))
    db.init_schema()
    conn = db.connect()
    conn.execute("INSERT INTO products (product_id, name, price, stock, floor_price) "
                 "VALUES (?,?,?,?,?)", (SKU, "Nike Air Max 270", LIST_PRICE, 99, FLOOR))
    conn.commit()
    conn.close()
    monkeypatch.setattr(db_mod, "_DB", db)

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    # hmdp 同时带 id(下单用)与 sku(议价用)——两套标识的桥
                    return {"success": True,
                            "data": {"id": 1, "title": "Nike Air Max 270",
                                     "price": int(LIST_PRICE * 100), "stock": 99,
                                     "sku": SKU}}
            return R()

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: C())

    from app.api.app import create_app
    return TestClient(create_app()), db


def _buy(c, qty=1):
    return c.post("/api/order", json={"item_id": "1", "quantity": qty})


def _order_price(db, order_id):
    return db.get_order(order_id)["total"]


# --------------------------------------------------------------------------
# 核心:谈成的价要真的收到
# --------------------------------------------------------------------------

def test_bargained_price_applies_to_order(client):
    """**核心断言。** 修复前这里会是 899——客服答应了 780,系统收 899。"""
    c, db = client
    db.record_bargain_deal("default", SKU, DEAL)

    oid = _buy(c).json()["order_id"]
    assert _order_price(db, oid) == DEAL


def test_without_a_deal_the_list_price_is_used(client):
    """**反向断言**:没谈过价就按标价。没有这条,"永远用某个低价"也能让上面那条过。"""
    c, db = client
    oid = _buy(c).json()["order_id"]
    assert _order_price(db, oid) == LIST_PRICE


def test_deal_applies_to_every_unit_in_the_order(client):
    """数量 > 1 时整单适用。

    这是个**产品决定**:买家谈的是单价,只给第 1 件会是另一种意外。一次性核销
    保证他不能靠一次议价按底价囤货。
    """
    c, db = client
    db.record_bargain_deal("default", SKU, DEAL)
    oid = _buy(c, qty=3).json()["order_id"]
    assert _order_price(db, oid) == round(DEAL * 3, 2)


def test_order_item_records_the_bargained_unit_price(client):
    """订单行里存的也要是成交单价,否则对账时对不上。"""
    c, db = client
    db.record_bargain_deal("default", SKU, DEAL)
    oid = _buy(c).json()["order_id"]
    assert db.get_order(oid)["items"][0]["price"] == DEAL


# --------------------------------------------------------------------------
# 一次性
# --------------------------------------------------------------------------

def test_deal_is_single_use(client):
    """一笔成交只兑一单;第二单恢复标价。

    不这样的话,买家谈一次就能按底价无限买。
    """
    c, db = client
    db.record_bargain_deal("default", SKU, DEAL)
    first = _buy(c).json()["order_id"]
    second = _buy(c).json()["order_id"]
    assert _order_price(db, first) == DEAL
    assert _order_price(db, second) == LIST_PRICE


def test_consume_is_conditional(client):
    """核销是条件更新:并发下单只有一个能兑到(与 pay_order/set_refund 同一套纪律)。"""
    c, db = client
    deal_id = db.record_bargain_deal("default", SKU, DEAL)
    assert db.consume_bargain_deal(deal_id, "ORD-1") is True
    assert db.consume_bargain_deal(deal_id, "ORD-2") is False


def test_a_failed_order_does_not_eat_the_deal(client, monkeypatch):
    """建单失败不该白吃掉买家一次成交——取价与核销是分开的两步。"""
    import httpx

    c, db = client
    db.record_bargain_deal("default", SKU, DEAL)

    class Broken:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): raise httpx.ConnectError("refused")

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: Broken())
    assert _buy(c).status_code == 503
    assert db.active_bargain_deal("default", SKU) is not None, "失败的下单吃掉了成交价"


# --------------------------------------------------------------------------
# 兑现时的重新校验:不信任存下来的数字
# --------------------------------------------------------------------------

def test_deal_below_current_floor_is_clamped(client):
    """底价在成交之后被调高 → 按新底价收,不按旧成交价。"""
    c, db = client
    db.record_bargain_deal("default", SKU, 700.0)      # 低于当前底价 750
    oid = _buy(c).json()["order_id"]
    assert _order_price(db, oid) == FLOOR


def test_deal_above_current_list_price_is_clamped(client):
    """标价降到成交价以下 → 按标价收。

    **绝不能因为"谈过价"反而收得更贵**:那是买家最不能接受的一种。
    """
    c, db = client
    db.record_bargain_deal("default", SKU, 1200.0)
    oid = _buy(c).json()["order_id"]
    assert _order_price(db, oid) == LIST_PRICE


def test_non_positive_ttl_is_rejected(client):
    """非正有效期明确报错,而不是让 expires_at 变成 NULL 撞 NOT NULL。

    (第一版测试用 `ttl_hours=-1` 走捷径造过期,拿到的是一条看不出原因的
    IntegrityError——那个报错本身就是个可用性问题,顺手修了。)
    """
    c, db = client
    with pytest.raises(ValueError, match="正小时数"):
        db.record_bargain_deal("default", SKU, DEAL, ttl_hours=0)


def test_expired_deal_is_ignored(client):
    """过期的成交价不生效(有效期由 settings.bargain_deal_ttl_hours 决定)。"""
    c, db = client
    db.record_bargain_deal("default", SKU, DEAL)
    conn = db.connect()          # 直接把到期时间拨到过去,不去滥用 ttl 参数
    conn.execute("UPDATE bargain_deals SET expires_at = '2000-01-01 00:00:00'")
    conn.commit(); conn.close()
    assert db.active_bargain_deal("default", SKU) is None
    oid = _buy(c).json()["order_id"]
    assert _order_price(db, oid) == LIST_PRICE


def test_deal_is_scoped_to_the_buyer(client):
    """别人谈的价不能被我用上。"""
    c, db = client
    db.record_bargain_deal("someone_else", SKU, DEAL)
    oid = _buy(c).json()["order_id"]
    assert _order_price(db, oid) == LIST_PRICE


def test_only_the_latest_unconsumed_deal_counts(client):
    """再谈一轮就覆盖上一轮:留着旧的会让"到底按哪个价"要看时间戳才能回答。"""
    c, db = client
    db.record_bargain_deal("default", SKU, 850.0)
    db.record_bargain_deal("default", SKU, DEAL)
    oid = _buy(c).json()["order_id"]
    assert _order_price(db, oid) == DEAL


def test_missing_sku_falls_back_to_list_price(client, monkeypatch):
    """hmdp 老版本不带 sku → 换算不出本地商品 → 按标价走。

    **宁可多收得对,不可少收得错**:取不到映射时不该猜。
    """
    c, db = client
    db.record_bargain_deal("default", SKU, DEAL)

    class NoSku:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "data": {"id": 1, "title": "鞋",
                                                      "price": int(LIST_PRICE * 100),
                                                      "stock": 9}}
            return R()

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: NoSku())
    oid = _buy(c).json()["order_id"]
    assert _order_price(db, oid) == LIST_PRICE


# --------------------------------------------------------------------------
# 议价工具侧:成交要落库,且 price_effect 要说实话
# --------------------------------------------------------------------------

def test_accept_with_consent_records_a_deal(client, monkeypatch):
    """拿到 deal_close 确认后必须落一笔成交,否则这个价格哪儿也去不了。"""
    from app.agent.consent import consent_scope
    from app.agent.runtime_context import set_current_user
    from app.agent.tools.bargain import negotiate_price

    c, db = client
    set_current_user("default")
    with consent_scope(["deal_close"]):
        out = negotiate_price(SKU, buyer_offer=850.0)

    assert out["decision"] == "accept"
    deal = db.active_bargain_deal("default", SKU)
    assert deal is not None and deal["price"] == out["suggested_price"]
    assert "自动生效" in out["price_effect"]


def test_accept_without_consent_records_nothing(client):
    """没确认就只是请求确认——**不能落库**,否则确认门形同虚设。"""
    from app.agent.runtime_context import set_current_user
    from app.agent.tools.bargain import negotiate_price

    c, db = client
    set_current_user("default")
    out = negotiate_price(SKU, buyer_offer=850.0)
    assert out.get("need_confirm") is True
    assert db.active_bargain_deal("default", SKU) is None


def test_counter_round_says_it_is_not_locked(client):
    """还价阶段的 price_effect 必须说"尚未成交",不能让模型说成锁价了。"""
    from app.agent.runtime_context import set_current_user
    from app.agent.tools.bargain import negotiate_price

    c, db = client
    set_current_user("default")
    out = negotiate_price(SKU, buyer_offer=500.0)
    assert out["decision"] != "accept"
    assert "尚未成交" in out["price_effect"]
    assert db.active_bargain_deal("default", SKU) is None


def test_no_user_identity_says_it_is_not_locked(client):
    """拿不到买家身份时落不了库——**必须如实说**,不能让模型以为锁价了。

    那正是这条缺陷最初的形态:客服说"下单自动生效",系统按标价收钱。
    """
    from app.agent.consent import consent_scope
    from app.agent.runtime_context import set_current_user
    from app.agent.tools.bargain import negotiate_price

    c, db = client
    set_current_user(None)
    with consent_scope(["deal_close"]):
        out = negotiate_price(SKU, buyer_offer=850.0)
    assert "未能锁定" in out["price_effect"]
    assert "禁止" in out["price_effect"]


# --------------------------------------------------------------------------
# hmdp 履约路径:议价价**用不了**,而且不能假装能用
#
# **实测缺陷**(拉起 Redis 之后真下了一单才发现)。demo 用户的订单建在 hmdp,而
# `POST {base}/order` 的入参只有 `productId/quantity/address`——**没有价格字段**。
# 于是那一单的实际形态是:
#
#     购物车显示「议价 ¥780」、按钮「去下单 ¥780」、我们的响应也回 total=780
#     hmdp 真实建单 total=89900 分 = ¥899
#     议价成交价还被核销掉了(consumed_at 有值、order_id 指向那笔 hmdp 单)
#
# **承诺 780、实收 899、券也没了**——比修复前那个诚实的 899 更糟。
#
# 判据放在 `_apply_bargain_price` 里而不是各调用点:购物车、商品详情、下单三处都调
# 它,一处判、三处一致。分散判早晚出现"购物车显示 780、结账收 899"。
#
# 这是 demo 模式专属的缺口:真实部署 demo_mode=False,所有订单走本地库,议价正常。
# 要让它在 hmdp 上生效,得 hmdp 支持接收成交价——那是另一个系统的改动,
# 这里不假装能做到。
# --------------------------------------------------------------------------

def test_hmdp_user_does_not_get_the_bargained_price(client, monkeypatch):
    """**核心断言。** 走 hmdp 的用户拿标价,不拿议价价——因为价格传不过去。"""
    import app.api.app as app_mod
    from app.config import settings as st

    c, db = client
    monkeypatch.setattr(st.settings, "demo_mode", True)
    monkeypatch.setattr(st.settings, "demo_hmdp_user_id", "hmdpuser")
    monkeypatch.setattr(st.settings, "demo_hmdp_token", "tok")
    db.record_bargain_deal("hmdpuser", SKU, DEAL)

    p = {"id": "1", "title": "鞋", "price": LIST_PRICE, "sku": SKU}
    assert app_mod._apply_bargain_price("hmdpuser", p, 1) == (LIST_PRICE, None)


def test_local_user_still_gets_the_bargained_price(client, monkeypatch):
    """**反向断言**:本地建单的用户照常生效。少了这条,"一律不给议价"也能让上面过。"""
    import app.api.app as app_mod
    from app.config import settings as st

    c, db = client
    monkeypatch.setattr(st.settings, "demo_mode", True)
    monkeypatch.setattr(st.settings, "demo_hmdp_user_id", "hmdpuser")
    db.record_bargain_deal("localuser", SKU, DEAL)

    p = {"id": "1", "title": "鞋", "price": LIST_PRICE, "sku": SKU}
    price, deal = app_mod._apply_bargain_price("localuser", p, 1)
    assert price == DEAL and deal is not None


def test_hmdp_path_does_not_consume_the_deal(client, monkeypatch):
    """不生效就**不能核销**——白吃掉买家一次议价额度是这条缺陷里最伤人的一半。"""
    import app.api.app as app_mod
    from app.config import settings as st

    c, db = client
    monkeypatch.setattr(st.settings, "demo_mode", True)
    monkeypatch.setattr(st.settings, "demo_hmdp_user_id", "hmdpuser")
    db.record_bargain_deal("hmdpuser", SKU, DEAL)

    p = {"id": "1", "title": "鞋", "price": LIST_PRICE, "sku": SKU}
    _, deal = app_mod._apply_bargain_price("hmdpuser", p, 1)
    app_mod._consume_bargain(deal, "ORD-X")          # deal 是 None,应当什么都不做
    assert db.active_bargain_deal("hmdpuser", SKU) is not None, "议价额度被白吃了"


def test_hmdp_token_helper_is_module_level(client):
    """判据必须是模块级、唯一一份。

    第一版把它写成引用 `create_app()` 里的闭包——import 能过、**一调就 NameError**。
    抄第二份更糟:"谁走 hmdp"有两份判断,迟早分叉成"购物车按本地算价、下单却建到
    了 hmdp"。
    """
    import app.api.app as app_mod

    assert callable(getattr(app_mod, "_hmdp_token_for", None))
