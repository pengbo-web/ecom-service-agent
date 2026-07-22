"""高频客服操作工具:改地址 / 取消订单 / 催发货 / 开发票 / 优惠券(向生产客服场景扩展)。

change_address / cancel_order 影响订单、涉钱或改重要信息 → 接 consent 前置授权门
(未授权返回 need_confirm,不执行;确认后由服务端重放)。
expedite_shipping / issue_invoice / query_coupons 低风险或只读 → 直接执行。
"""

from app.db import get_db
from app.agent.consent import is_allowed, need_confirm_result

# 未发货、可改地址/可取消的状态
_MUTABLE = {"pending"}


def change_address(order_id: str, new_address: str) -> dict:
    """修改订单收货地址（仅未发货订单）。敏感操作,需前置确认。"""
    db = get_db()
    order = db.get_order(order_id)
    if not order:
        return {"success": False, "error": f"未找到订单 {order_id}，请核实订单号"}
    if order["status"] not in _MUTABLE:
        return {"success": False, "error":
                f"订单当前状态为「{order['status']}」，已进入发货流程，无法修改收货地址；"
                f"如需变更请走退换货或联系人工。"}
    if not is_allowed("change_address"):
        return need_confirm_result(
            "change_address",
            f"修改收货地址是敏感操作。请确认将订单 {order_id} 的收货地址改为：{new_address}？"
            f"确认后我再为您提交。")
    db.set_shipping_address(order_id, new_address)
    return {"success": True, "message": f"订单 {order_id} 的收货地址已更新为：{new_address}。"}


def cancel_order(order_id: str) -> dict:
    """取消订单（仅未发货订单）。敏感操作,需前置确认。"""
    db = get_db()
    order = db.get_order(order_id)
    if not order:
        return {"success": False, "error": f"未找到订单 {order_id}，请核实订单号"}
    status = order["status"]
    if status == "cancelled":
        return {"success": False, "error": "该订单已取消，无需重复操作。"}
    if status not in _MUTABLE:
        return {"success": False, "error":
                f"订单当前状态为「{status}」，已发货/已完成，无法直接取消；如需退货请使用退款流程。"}
    if not is_allowed("cancel_order"):
        return need_confirm_result(
            "cancel_order",
            f"取消订单是敏感操作。请确认取消订单 {order_id}（金额 ¥{order.get('total')}）？"
            f"确认后我再为您提交。")
    db.update_order_status(order_id, "cancelled")
    return {"success": True, "message":
            f"订单 {order_id} 已成功取消。若已付款，款项将原路退回，1-3 个工作日到账。"}


def expedite_shipping(order_id: str) -> dict:
    """催发货/加急（低风险请求,直接受理）。"""
    db = get_db()
    order = db.get_order(order_id)
    if not order:
        return {"success": False, "error": f"未找到订单 {order_id}，请核实订单号"}
    status = order["status"]
    if status == "pending":
        return {"success": True, "message":
                f"已为订单 {order_id} 提交加急出库请求，仓库将优先处理，预计更快发货。"}
    if status == "shipped":
        return {"success": True, "message":
                f"订单 {order_id} 已发货，正在运输途中；如急需可在物流详情中申请加急派送。"}
    return {"success": False, "error": f"订单当前状态为「{status}」，无需加急。"}


def issue_invoice(order_id: str, title: str = "个人", tax_id: str = "") -> dict:
    """开具电子发票（只读:据订单生成发票信息）。"""
    db = get_db()
    order = db.get_order(order_id)
    if not order:
        return {"success": False, "error": f"未找到订单 {order_id}，请核实订单号"}
    if order["status"] in ("pending", "cancelled"):
        return {"success": False, "error":
                f"订单当前状态为「{order['status']}」，暂不可开票（需完成支付且未取消）。"}
    items = order.get("items", [])
    return {
        "success": True,
        "invoice": {
            "order_id": order_id,
            "type": "电子普通发票",
            "title": title,
            "tax_id": tax_id or None,
            "amount": order.get("total"),
            "items": [
                {"name": it.get("name"), "quantity": it.get("quantity"), "price": it.get("price")}
                for it in items
            ],
        },
        "message": f"电子发票已开具（抬头：{title}，金额 ¥{order.get('total')}），将发送至您的账户/邮箱。",
    }


# 可用优惠券(mock 数据)
_COUPONS = [
    {"code": "NEW20", "name": "新人券", "discount": "满100减20", "expires": "2026-12-31"},
    {"code": "VIP90", "name": "会员9折券", "discount": "9折(最高减50)", "expires": "2026-09-30"},
    {"code": "SHOE30", "name": "鞋类专享券", "discount": "满300减30", "expires": "2026-08-31"},
]


def query_coupons() -> dict:
    """查询当前可用优惠券（只读）。"""
    return {"success": True, "coupons": _COUPONS,
            "note": "以上为当前可用优惠券,下单结算时可选用。"}
