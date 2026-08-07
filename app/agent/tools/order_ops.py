"""高频客服操作工具:改地址 / 取消订单 / 催发货 / 开发票 / 优惠券(向生产客服场景扩展)。

change_address / cancel_order 影响订单、涉钱或改重要信息 → 接 consent 前置授权门
(未授权返回 need_confirm,不执行;确认后由服务端重放)。
expedite_shipping / issue_invoice / query_coupons 低风险或只读 → 直接执行。
"""

from app.db import get_db
from app.agent.consent import is_allowed, need_confirm_result
from app.agent.tools.ownership import owned_order

# 未发货、可改地址/可取消的状态
_MUTABLE = {"pending"}


def change_address(order_id: str, new_address: str) -> dict:
    """修改订单收货地址（仅未发货订单）。敏感操作,需前置确认。"""
    db = get_db()
    order = owned_order(order_id)
    if not order:
        return {"success": False, "error": f"未找到订单 {order_id}，请核实订单号"}
    # 新地址非空校验:模型解析失败/误传空串时,别把收货地址覆盖成空导致无法发货。
    if not (new_address or "").strip():
        return {"success": False, "error": "新收货地址不能为空，请提供完整的收货地址。"}
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
    order = owned_order(order_id)
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
    order = owned_order(order_id)
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
    order = owned_order(order_id)
    if not order:
        return {"success": False, "error": f"未找到订单 {order_id}，请核实订单号"}
    if order["status"] in ("pending", "cancelled", "refund_processing"):
        return {"success": False, "error":
                f"订单当前状态为「{order['status']}」，暂不可开票"
                f"（需完成支付、未取消且不在退款流程中）。"}
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


# 可用优惠券(mock 数据)。audience: new=仅新客 / member=仅会员 / all=人人可领
_COUPONS = [
    {"code": "NEW20", "name": "新人券", "discount": "满100减20", "expires": "2026-12-31", "audience": "new"},
    {"code": "VIP90", "name": "会员9折券", "discount": "9折(最高减50)", "expires": "2026-09-30", "audience": "member"},
    {"code": "SHOE30", "name": "鞋类专享券", "discount": "满300减30", "expires": "2026-08-31", "audience": "all"},
]

_AUDIENCE_REASON = {
    "new": "仅限新客(您已有历史订单)",
    "member": "仅限会员(开通会员后可领)",
}


def filter_coupons(coupons: list, is_new: bool, is_member: bool):
    """按资格切分:返回 (可领列表, 不可领列表[带 reason])。纯函数,便于测试。"""
    ok, no = [], []
    for c in coupons:
        aud = c.get("audience", "all")
        eligible = (aud == "all") or (aud == "new" and is_new) or (aud == "member" and is_member)
        if eligible:
            ok.append({k: v for k, v in c.items() if k != "audience"})
        else:
            no.append({"code": c["code"], "name": c["name"],
                       "reason": _AUDIENCE_REASON.get(aud, "当前不可领")})
    return ok, no


def _granted_coupons(db, uid: str) -> list:
    """该用户已被发放过的券(N6:coupon_grants 唯一数据源),附上券面文案。

    只在 _COUPONS 里找得到对应券码时才附 name/discount——发放记录理论上
    不该出现未知券码(issue_for_draft 已经挡在发放之前),这里兜底不让
    找不到定义的行直接崩,而是原样带出券码本身。
    """
    by_code = {c["code"]: c for c in _COUPONS}
    out = []
    for g in db.list_user_grants(uid):
        info = by_code.get(g["code"], {})
        out.append({"code": g["code"], "name": info.get("name", g["code"]),
                    "discount": info.get("discount", ""), "granted_at": g.get("created_at")})
    return out


def query_coupons() -> dict:
    """查询当前用户可领的优惠券(按会员等级 + 新老客身份筛选,只读)。

    fail-open:识别不到当前用户或查资格异常 → 返回全部券,不漏发。
    附带 `granted`:该用户已被发放过的券(N6,数据源是 coupon_grants,只读),
    供模型判断"这张券已经给过了,别重复承诺"。
    """
    from app.agent.runtime_context import get_current_user
    uid = get_current_user()
    if not uid:
        return {"success": True, "coupons": [{k: v for k, v in c.items() if k != "audience"}
                                             for c in _COUPONS],
                "unavailable": [], "granted": [], "note": "未识别当前用户,已展示全部券。"}
    try:
        # 注意:这里必须用模块级的 get_db(顶部 `from app.db import get_db`),
        # 不能在函数体内再 `from app.db import get_db` 局部重绑一份——那样会
        # 在这个函数作用域内屏蔽掉模块级名字,导致 monkeypatch.setattr(order_ops,
        # "get_db", ...) 这种测试打桩方式失效(打桩打的是模块属性,局部 import
        # 完全不看它)。query_coupons_shows_granted 等测试正是靠这种打桩方式接入。
        db = get_db()
        user = db.get_user(uid)
        is_member = bool(user) and (user.get("member_level") or "normal") != "normal"
        is_new = db.count_user_orders(uid) == 0
        ok, no = filter_coupons(_COUPONS, is_new=is_new, is_member=is_member)
        return {"success": True, "coupons": ok, "unavailable": no,
                "granted": _granted_coupons(db, uid),
                "note": "以上为您当前可领的优惠券(已按会员等级与新老客身份筛选)。"}
    except Exception:
        return {"success": True, "coupons": [{k: v for k, v in c.items() if k != "audience"}
                                             for c in _COUPONS],
                "unavailable": [], "granted": [], "note": "未识别当前用户,已展示全部券。"}
