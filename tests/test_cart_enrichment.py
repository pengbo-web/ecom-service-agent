"""购物车必须带商品信息。

改造前 `/api/cart` 只返回购物车行本身(sku/quantity/status),于是页面上一件商品
只显示一个原始 sku(如「1」)。最严重的后果不是难看:页面上那个「去下单」按钮
会**在买家从未看到价格的情况下提交订单**,下完单才用弹窗告知金额——那是让人
闭着眼睛付钱。
"""

from __future__ import annotations

import inspect

from app.api import app as app_module


def _cart_endpoint_source() -> str:
    src = inspect.getsource(app_module)
    start = src.index("def get_cart_endpoint")
    end = src.index('@app.put("/api/cart/{sku}")', start)
    return src[start:end]


def test_cart_is_enriched_on_the_server_side():
    """商品信息在后端补,不让前端自己去 join。

    购物车里的价格必须与商城页、与最终下单金额同源;前端各自查一次迟早对不上。
    """
    body = _cart_endpoint_source()
    assert "_fetch_hmdp_product" in body, "应复用与商城/下单同一条商品读取路径"
    assert '"subtotal"' in body, "小计应由后端算,不让前端各算各的"


def test_missing_product_keeps_the_row_and_is_marked():
    """商品查不到时保留该行并标记,而不是丢掉或填 0。

    商品下架、商品服务抖动都可能查不到,而买家的购物车行是他自己加的,不该
    因为一次查询失败就从界面上消失。价格给 None 而不是 0——0 会被渲染成
    「¥0」,让买家以为免费。
    """
    body = _cart_endpoint_source()
    assert '"product_missing": True' in body
    assert '"price": None' in body, "查不到时价格必须是 None,不能是 0"


def test_price_is_not_defaulted_to_zero_anywhere():
    body = _cart_endpoint_source()
    assert '"price": 0' not in body
