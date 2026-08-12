"""议价谈成的价格**不会作用于订单**——所以不许对买家说"已锁定/自动生效"。

**实测缺陷**(走查议价时抓到,是这一整轮里对买家伤害最直接的一条)。

四轮压价(老客户 / 去别家 / 投诉威胁)下来,**底价守住了**——`compute_offer` 是纯函数,
带一条显式不变量 `price = max(price, F)  # 永不破底`,谈到 ¥750(标价 ¥899)就不再降。
工具本身没问题。

问题在**话术与系统能力对不上**。第一轮客服自己说:

> 该价格为平台授权的最优让利，**下单时将自动生效**。

而事实是:

- `negotiate_price` 成交时只做一件事——把价格写进 `bargain_sessions.last_offer`,
  **不创建订单、不设任何价格锁**;
- 下单路径 `POST /api/order` 对议价状态的引用次数是 **0**,它按
  `total = round(p["price"] * qty, 2)` **直接用商品标价**结算;
- `bargain_sessions` 全仓只被三处读:议价工具自己(算轮次)、商机发现(把未成交议价
  当商机)、会话结束时清除。**没有任何下单/支付路径读它。**

也就是说:买家谈到 ¥750 之后直接下单,会被按 **¥899** 收钱。

这比之前抓到的"平台承担换货运费"更硬——那句还只是费用归属,**这句是买家实际付多少**。

修的是"不许承诺兑现不了的事",不是"让它兑现":真正让议价价格作用于订单,需要打通本地
`product_id` 与 hmdp `item_id` 两套标识(与 `product_ref.py` 记的是同一个命名空间问题),
那是一次数据架构改动,单独跟进。
"""

import pytest

from app.agent.tools.bargain import compute_offer


# --------------------------------------------------------------------------
# 底价:这一半是好的,先钉住别改坏
# --------------------------------------------------------------------------

@pytest.mark.parametrize("buyer_offer", [700.0, 600.0, 500.0, 1.0, 0.0, -100.0])
def test_never_below_floor(buyer_offer):
    """不变量:任何出价都不会让建议价跌破底价。"""
    r = compute_offer(list_price=899.0, floor_price=750.0,
                      buyer_offer=buyer_offer, rounds=0)
    assert r["suggested_price"] >= 750.0


def test_ladder_never_below_floor_at_any_round():
    """多轮让价也不破底——实测压了 4 轮(老客户/去别家/投诉威胁)都守住了。"""
    for rounds in range(0, 12):
        r = compute_offer(list_price=899.0, floor_price=750.0,
                          buyer_offer=None, rounds=rounds)
        assert r["suggested_price"] >= 750.0


def test_offer_at_or_above_list_price_is_accepted_at_list():
    """买家出价高于标价时按标价成交,不多收。"""
    r = compute_offer(list_price=899.0, floor_price=750.0,
                      buyer_offer=1000.0, rounds=0)
    assert r["decision"] == "accept"
    assert r["suggested_price"] == 899.0


def test_default_floor_ratio_applies_when_no_floor_column():
    """没配 floor_price 时按 bargain_floor_ratio 推导,同样不破。"""
    from app.config.settings import settings
    r = compute_offer(list_price=1000.0, floor_price=None,
                      buyer_offer=1.0, rounds=0)
    assert r["suggested_price"] >= round(1000.0 * settings.bargain_floor_ratio, 2)


# --------------------------------------------------------------------------
# 话术:不许承诺系统兑现不了的事
# --------------------------------------------------------------------------

def test_tool_result_states_the_price_is_not_applied(monkeypatch, tmp_path):
    """工具返回值里必须写明"不会自动作用于订单"。

    **放进返回值而不是只写 prompt 规则**:这个项目自己验证过 prompt 保得住动作、
    保不住话术。模型看得到这条事实,就不需要自己脑补生效方式——实测它脑补出来的是
    "下单时将自动生效"。
    """
    from app.agent.tools import bargain
    from app.db.database import Database

    db = Database(db_path=str(tmp_path / "b.db"))
    db.init_schema()
    conn = db.connect()
    try:
        conn.execute("INSERT INTO products (product_id, name, price, stock) "
                     "VALUES ('P1', '测试鞋', 899.0, 10)")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(bargain, "get_db", lambda: db)

    out = bargain.negotiate_price("P1", buyer_offer=None)
    assert out["success"] is True
    eff = out.get("price_effect") or ""
    assert "不会自动作用于订单" in eff
    assert "禁止" in eff and "自动生效" in eff, "要明确禁止那句实测出现过的话术"


def test_deal_close_reply_does_not_promise_an_order():
    """成交回复不许说"锁定/即将生成订单"——系统并不会那么做。"""
    from app.api.streaming import _build_confirm_reply

    text = _build_confirm_reply("deal_close", {
        "success": True, "suggested_price": 750.0, "product_name": "Nike Air Max 270"})
    assert "750" in text
    for forbidden in ("锁定", "即将为您生成订单", "自动生效"):
        assert forbidden not in text, f"承诺了系统做不到的事: {forbidden}"
    # 必须说清真实后果,否则买家仍会以为按谈成价下单
    assert "标价" in text, "要告诉买家直接下单会按标价结算"
    assert "客服" in text, "要给出让这个价生效的实际路径"


def test_other_confirm_replies_unchanged():
    """只改 deal_close 这一支,别的风险动作回复逐字不变。"""
    from app.api.streaming import _build_confirm_reply

    assert _build_confirm_reply("refund", {"success": True, "message": "退款已提交"}) \
        == "✅ 退款已提交"
    assert _build_confirm_reply("cancel_order", {"success": True, "message": "已取消"}) \
        == "✅ 已取消"
    assert "无法完成" in _build_confirm_reply("deal_close", {"success": False})


def test_order_path_really_ignores_bargain():
    """把"下单不认议价"这个事实钉住:它是上面所有文案约束的**前提**。

    哪天有人真的在下单侧接上了议价价格,这条会失败——那时应当回来把文案改回
    "已锁定",而不是删掉这条断言。
    """
    import ast
    import inspect
    import app.api.app as app_mod

    src = inspect.getsource(app_mod)
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "create_order"), None)
    assert fn is not None, "create_order 改名了,这条断言需要跟着改"
    body = ast.get_source_segment(src, fn) or ""
    for token in ("bargain", "last_offer", "成交价"):
        assert token not in body, (
            f"下单路径现在引用了 {token} —— 议价价格可能已经能作用于订单了,"
            "请回头把成交话术改回去")
