from app.db import get_db
from app.agent.consent import is_allowed, need_confirm_result
from app.agent.tools.ownership import owned_order


def apply_refund(order_id: str, reason: str) -> dict:
    """为指定订单申请退款，需提供退款原因。"""
    db = get_db()
    order = owned_order(order_id)
    if not order:
        return {"success": False, "error": f"未找到订单 {order_id}，请核实订单号"}

    if order["status"] == "refund_processing":
        return {"success": False, "error": "该订单已有退款申请正在处理中，请耐心等待"}

    # **未支付的订单不存在"退款"**——没收到的钱退不回去。
    #
    # 实测(走查并发与幂等时):`unpaid` 订单调本工具会**放行**,状态被改成
    # refund_processing,并对买家说"退款申请已提交…预计 1-3 个工作日内审核完成"。
    # 两重后果:
    # ① 对一分钱没付的买家承诺了一笔退款;
    # ② 状态离开 unpaid 之后 `pay_order`(条件是 status='unpaid')永远不再生效
    #    ——**这笔单从此付不了款**,而买家本来只是"不想买了"。
    #
    # 而 `unpaid_flow_enabled=True` 时本地订单的起始状态就是 unpaid,这是默认路径。
    #
    # 这里只拒绝并指路,**不替买家改成取消**:取消是另一个动作、有它自己的确认门
    # (`consent` 里的 cancel),拿"退款"的授权去做"取消"等于绕过那道门。
    if order["status"] == "unpaid":
        return {"success": False, "error":
                f"订单 {order_id} 尚未支付，不产生退款（没有款项需要退回）。"
                "如果不想要了，我可以帮您直接取消这笔订单——需要的话告诉我。"}

    # 已取消的订单在 cancel_order 时已承诺"款项原路退回",此处再退即二次退款,拦下。
    if order["status"] == "cancelled":
        return {"success": False, "error":
                "该订单已取消，款项将原路退回（1-3 个工作日到账），无需重复申请退款"}

    # 前置授权门:退款是涉钱不可逆动作,未获本轮确认则不执行
    if not is_allowed("refund"):
        return need_confirm_result(
            "refund",
            f"退款是敏感操作。请确认是否为订单 {order_id} 办理退款（原因：{reason}）？"
            "确认后我再为您提交。",
        )

    was_pending = order["status"] == "pending"
    # `set_refund` 现在是条件更新(只对可退状态生效),返回值**必须看**。
    #
    # 上面那些 status 判断是"先读后写",读到写之间订单状态可能已经被另一个请求改掉。
    # 实测 16 个并发申请,**12 个都返回了成功**并各自对买家说"退款申请已提交"。
    # 条件更新让并发里只有一个能赢;输的那些在这里如实转成"已有申请在处理中",
    # 而不是继续往下走那句报喜的话。
    if not db.set_refund(order_id, reason):
        return {"success": False,
                "error": "该订单已有退款申请正在处理中，请耐心等待"}

    if was_pending:
        return {
            "success": True,
            "message": (
                f"订单 {order_id} 尚未发货，已直接取消并发起退款。"
                f"退款原因：{reason}。退款将在 1-3 个工作日内原路退回。"
            ),
        }

    return {
        "success": True,
        "message": (
            f"退款申请已提交。订单 {order_id}，退款原因：{reason}。"
            f"预计 1-3 个工作日内审核完成，届时会通知您退货地址。"
        ),
    }
