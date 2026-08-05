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
        }
        for o in raw
    ]
    return {"success": True, "count": len(orders), "orders": orders}
