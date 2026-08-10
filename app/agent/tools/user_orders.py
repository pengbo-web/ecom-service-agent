from app.db import get_db

STATUS_LABELS = {
    "unpaid": "待支付",
    "pending": "待发货",
    "shipped": "已发货",
    "delivered": "已签收",
    "refund_processing": "退款中",
}


def list_user_orders() -> dict:
    """查询【当前用户】的订单概要列表(auth 开时按登录身份隔离)。"""
    from app.config.settings import settings
    raw = get_db().list_orders()
    if settings.auth_enabled:
        from app.agent.runtime_context import get_current_user
        uid = get_current_user()
        raw = [o for o in raw if uid and o.get("user") == uid]
    orders = [
        {
            "order_id": o["order_id"],
            "status": STATUS_LABELS.get(o["status"], o["status"]),
            "items_summary": "、".join(item["name"] for item in o["items"]),
            "total": o["total"],
            "created_at": o["created_at"],
            # 与 MCP 版同一口径:让"这单没有物流单号"和"这份清单不带物流字段"
            # 成为两件可分辨的事。清单静默丢字段等于邀请模型把"我没查"说成
            # "查不到"——实测 Agent 就是这样对买家说出"多次查询均未匹配到物流
            # 单号"的,而它一次都没查过。见 mcp_server/hmdp_server.py 同名函数。
            "has_tracking": bool(o.get("tracking_number")),
        }
        for o in raw
    ]
    return {"success": True, "count": len(orders), "orders": orders,
            "note": "概要清单，不含物流轨迹与商品明细；"
                    "has_tracking 为 true 的订单可用 query_logistics 查轨迹"}
