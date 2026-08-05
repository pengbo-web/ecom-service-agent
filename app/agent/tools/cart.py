"""购物车工具(买家用):加购 / 查看。

**不含结算**——下单仍然走既有的自助下单路径(前端商品卡「立即购买」/购物车
「去下单」都是买家自己在前端点出来的,最终都打到 `POST /api/order`)。这与
"Agent 不代客下单"是同一条底线:购物车只负责收集意向,真正的成交动作
(建单、付款)必须由买家自己完成,本模块因此**刻意不提供** checkout/place_order
这类工具。
"""

from __future__ import annotations

from app.db import get_db


def _current_user_id() -> str:
    """解析当前买家身份,不信任模型传参(与 query_coupons/list_user_orders 同门控):
    auth 开→只信运行时上下文里的登录身份,取不到就是空字符串,由调用方拒绝;
    auth 关→回落 "default"(教学/测试口径,与其它买家工具一致)。"""
    from app.config.settings import settings
    if not settings.auth_enabled:
        return "default"
    from app.agent.runtime_context import get_current_user
    return get_current_user() or ""


def add_to_cart(item_id: str, quantity: int = 1) -> dict:
    """把商品加入购物车。同一 sku 重复加购会**累加**数量,不会产生重复行。"""
    uid = _current_user_id()
    if not uid:
        return {"success": False, "error": "未识别当前用户,请先登录后再试"}
    sku = (item_id or "").strip()
    if not sku:
        return {"success": False, "error": "商品ID不能为空"}
    qty = max(1, int(quantity or 1))
    get_db().add_to_cart(uid, sku, qty)
    return {"success": True, "message": f"已加入购物车:{sku} x{qty}"}


def view_cart() -> dict:
    """查看当前用户的购物车(只读)。"""
    uid = _current_user_id()
    if not uid:
        return {"success": True, "count": 0, "items": []}
    items = get_db().list_cart(uid)
    return {"success": True, "count": len(items), "items": items}
