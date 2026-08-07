"""营销增长 Agent 的工具:找商机 + **起草**触达话术。

**唯一写路径是 create_outreach_draft(status='draft')**。本模块任何函数都不得
调用消息发送通道——发送只发生在管理端的审批端点里,且必须有人点过"批准"。
这是本项目与"全自动营销"方案的分界:向真实买家发消息是不可逆的对外动作,
必须落在既有的"不可逆动作需人工授权"这条线内。

注入面:商机数据(商品名/退款原因/收货地址)与店主输入都可能含指令性文本。
兜底不是"检出注入",而是**产物形态**——最坏情况也只是一条待审草稿。
承诺类敏感词额外标红,逼人工重点看。

口径说明(N5 更新)——本项目现在**有真实的未支付态**:`Database.create_order`
的 `status` 参数由调用方按 `settings.unpaid_flow_enabled` 传入,开关开时自助
下单落库恒为 `unpaid`,买家须调用 `pay_order` 完成支付才会推进到 `pending`
(待发货,付款已完成)。因此"催付款"(`unpaid_order`)与"弃单挽回"
(`abandoned_cart`,数据来自真实的 `carts` 表)这两个商机现在都有真实数据
支撑。旧 kind `stale_pending_order` 的语义相应**收窄**:它现在专指"已付款
但久未发货",不再兼指未支付——两者在 pending/unpaid 是两个不同状态值之后,
已经不会互相污染。
"""

from __future__ import annotations

from typing import Optional

from app.agent.tools.user_orders import STATUS_LABELS
from app.config.settings import settings
from app.db import get_db

# 支持的商机类型。未知 kind **拒绝**而不是猜一个,否则模型写错一个词就静默取错人群。
OPPORTUNITY_KINDS = {
    "stale_pending_order": "下单后久未推进(已付款待发货)",
    "unpaid_order": "下单未支付",
    "abandoned_cart": "加购未下单",
    "stalled_bargain": "议价未成交",
    "consulted_no_order": "咨询过但没下单",
    "shipped_no_care": "已发货待关怀",
    "delivered_no_review": "已签收未评价",
}

# "久拖不发"的判定:状态取自真实写路径(Database.create_order 的默认值),
# 滞后阈值单独具名成模块常量,不当魔法数散落在 SQL 里。
_STALE_PENDING_STATUS = "pending"
_STALE_PENDING_HOURS = 48


def _validate_kind(kind: str) -> Optional[dict]:
    """kind 合法性校验,find_opportunities/draft_outreach 共用同一份口径。"""
    if kind not in OPPORTUNITY_KINDS:
        return {"success": False,
                "error": f"未知的 kind「{kind}」,可选: {'、'.join(OPPORTUNITY_KINDS)}"}
    return None


def _commitment_hits(text: str) -> list[str]:
    """命中的金钱承诺词。复用 skill 风险分级的同一份词表,口径统一。

    注意:这只是朴素子串匹配,插个空格或标点就能绕过去——这个残余漏洞是可接受的,
    因为每条草稿都必须经人工审批才会发出,漏检的代价止步于"人工没被标红提醒",
    而不是消息真的发出去了。不要指望这里做成一道安全防线。
    """
    from app.agent.skills.risk import COMMITMENT_KEYWORDS
    return [w for w in COMMITMENT_KEYWORDS if w in (text or "")]


def _trim_and_classify(text: str) -> tuple[str, str]:
    """返回 (去空白后的原文, 需人工重点复核的原因)。

    这里**不做任何清洗/改写**,只是 trim + 按敏感词打标——所以不叫 sanitize:
    叫 sanitize 会让后来者误以为内容已被清洗过滤,从而放松警惕。命中承诺词
    也刻意不删改:删了店主就看不到 Agent 原本想说什么,标红交人工判断,比
    悄悄改写更诚实。
    """
    clean = (text or "").strip()
    hits = _commitment_hits(clean)
    if hits:
        return clean, "包含金钱承诺词: " + "、".join(hits[:5])
    return clean, ""


def find_opportunities(kind: str = "stale_pending_order", window_days: int = 14,
                       limit: int = 20) -> dict:
    """按类型找商机。只读。"""
    err = _validate_kind(kind)
    if err is not None:
        return err

    days = max(1, int(window_days))
    lim = max(1, min(int(limit), 100))
    conn = get_db().connect()
    try:
        if kind == "stale_pending_order":
            rows = conn.execute(
                f"SELECT o.order_id, o.user AS user_id, o.status, o.total, o.created_at, "
                f"       GROUP_CONCAT(oi.name, '、') AS items "
                f"FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.order_id "
                f"WHERE o.status = ? "
                f"  AND o.created_at <= datetime('now', '-{_STALE_PENDING_HOURS} hours') "
                f"  AND o.created_at >= datetime('now', '-{days} days') "
                f"GROUP BY o.order_id ORDER BY o.created_at DESC LIMIT ?",
                (_STALE_PENDING_STATUS, lim)).fetchall()
            # situation_label/order_status(_label) 是把"这是什么商机、订单现在
            # 到底是什么状态"下沉到每一条 item 里,而不是只留在顶层 kind_label——
            # handle_insight 传给 _llm_draft 的只有单条 opportunity dict,顶层
            # 字段它根本看不到。order_status_label 复用 user_orders.STATUS_LABELS
            # 这份唯一口径,不在这里另起一份映射,避免两处措辞后续走岔。
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": r["order_id"], "user_id": r["user_id"],
                      "order_status": r["status"],
                      "order_status_label": STATUS_LABELS.get(r["status"], r["status"]),
                      "amount": float(r["total"] or 0.0), "created_at": r["created_at"],
                      "items": r["items"] or ""} for r in rows]

        elif kind == "stalled_bargain":
            # bargain_sessions.session_id 与 conversations.conversation_id 是同一命名
            # 空间:EcomAgent.chat() 用同一个会话 id 既经 set_current_session 供议价工具
            # 写 bargain_sessions,又是 api/conversations.py::ensure_active 落进
            # conversations 表的那个 id。这里用 JOIN 把它解析成真实买家 user_id——
            # 解析不出来的会话(没有对应 conversations 行)宁可漏掉,也不能把
            # session_id 冒充 user_id 塞进草稿的收件地址,那样会寄给一个不存在的账号。
            rows = conn.execute(
                f"SELECT b.session_id, b.product_id, b.rounds, b.last_offer, "
                f"       b.updated_at, c.user_id AS buyer_id "
                f"FROM bargain_sessions b "
                f"JOIN conversations c ON c.conversation_id = b.session_id "
                f"WHERE b.updated_at >= datetime('now', '-{days} days') AND b.rounds > 0 "
                f"ORDER BY b.updated_at DESC LIMIT ?", (lim,)).fetchall()
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": "", "user_id": r["buyer_id"],
                      "session_id": r["session_id"], "product_id": r["product_id"],
                      "rounds": int(r["rounds"] or 0), "last_offer": r["last_offer"],
                      "created_at": r["updated_at"]} for r in rows]

        elif kind == "unpaid_order":
            # 催付款:status='unpaid' 且超过 settings.unpaid_stale_hours——刚下单
            # 还没到催的时候(买家可能就在结账流程里),阈值以内一律不算商机。
            # order_status(_label) 同样复用 STATUS_LABELS 这份唯一口径,让起草
            # 模型据此判断买家真实进度是"没付钱",不是"已付款等发货"。
            rows = conn.execute(
                f"SELECT o.order_id, o.user AS user_id, o.status, o.total, o.created_at, "
                f"       GROUP_CONCAT(oi.name, '、') AS items "
                f"FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.order_id "
                f"WHERE o.status = 'unpaid' "
                f"  AND o.created_at <= datetime('now', '-{max(1, int(settings.unpaid_stale_hours))} hours') "
                f"  AND o.created_at >= datetime('now', '-{days} days') "
                f"GROUP BY o.order_id ORDER BY o.created_at DESC LIMIT ?", (lim,)).fetchall()
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": r["order_id"], "user_id": r["user_id"],
                      "order_status": r["status"],
                      "order_status_label": STATUS_LABELS.get(r["status"], r["status"]),
                      "amount": float(r["total"] or 0.0), "created_at": r["created_at"],
                      "items": r["items"] or ""} for r in rows]

        elif kind == "abandoned_cart":
            # 弃单挽回:carts.status='active' 且 added_at 超过 settings.cart_stale_hours。
            # 数据来自真实的 carts 表(N5 新增),不再是"永远查不到一行"的占位符。
            rows = conn.execute(
                f"SELECT user_id, sku, quantity, added_at FROM carts "
                f"WHERE status = 'active' "
                f"  AND added_at <= datetime('now', '-{max(1, int(settings.cart_stale_hours))} hours') "
                f"  AND added_at >= datetime('now', '-{days} days') "
                f"ORDER BY added_at DESC LIMIT ?", (lim,)).fetchall()
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": "", "user_id": r["user_id"], "sku": r["sku"],
                      "quantity": int(r["quantity"] or 0), "created_at": r["added_at"]}
                     for r in rows]

        elif kind == "shipped_no_care":
            # 已发货待关怀:买家包裹动了,但店铺从没主动说过一句——现在只能被动
            # 应答"我的包裹到哪了"。status='shipped' 且 shipped_at 超过
            # settings.shipped_care_hours(刚发货就打扰没意义,包裹可能还没
            # 真正上路)。tracking_number/carrier/estimated_delivery 直接带
            # 上,这条商机存在的意义就是让起草模型说得出具体的物流信息,不是
            # 空喊一句"已发货哦"。
            rows = conn.execute(
                f"SELECT o.order_id, o.user AS user_id, o.status, o.total, o.shipped_at, "
                f"       o.tracking_number, o.carrier, o.estimated_delivery, "
                f"       GROUP_CONCAT(oi.name, '、') AS items "
                f"FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.order_id "
                f"WHERE o.status = 'shipped' "
                f"  AND o.shipped_at <= datetime('now', '-{max(1, int(settings.shipped_care_hours))} hours') "
                f"  AND o.shipped_at >= datetime('now', '-{days} days') "
                f"GROUP BY o.order_id ORDER BY o.shipped_at DESC LIMIT ?", (lim,)).fetchall()
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": r["order_id"], "user_id": r["user_id"],
                      "order_status": r["status"],
                      "order_status_label": STATUS_LABELS.get(r["status"], r["status"]),
                      "tracking_number": r["tracking_number"] or "",
                      "carrier": r["carrier"] or "",
                      "estimated_delivery": r["estimated_delivery"] or "",
                      "amount": float(r["total"] or 0.0), "created_at": r["shipped_at"],
                      "items": r["items"] or ""} for r in rows]

        elif kind == "delivered_no_review":
            # 已签收未评价:WHERE/NOT EXISTS 这两行逐字照抄
            # `Database.reviewable_items` 里"已签收且该 (order_id, sku) 尚未
            # 评价"这条唯一判定——不重写第二份口径,防止两处"该不该邀评"的
            # 标准悄悄走岔。这里在其之上只多加了两件事:①按 delivered_at 加
            # settings.review_request_hours 门槛(签收当天就催显得急功近利);
            # ②按 order 聚合(而不是像 reviewable_items 那样按 (order_id, sku)
            # 逐行返回),因为这里要产出的是"要不要联系这个买家"的商机,单位
            # 是订单/买家,不是逐个 sku。
            rows = conn.execute(
                f"SELECT o.order_id, o.user AS user_id, o.status, o.delivered_at, "
                f"       GROUP_CONCAT(oi.name, '、') AS items "
                f"FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
                f"WHERE o.status = 'delivered' "
                f"  AND NOT EXISTS (SELECT 1 FROM reviews r "
                f"                  WHERE r.order_id = o.order_id AND r.sku = oi.sku) "
                f"  AND o.delivered_at <= datetime('now', '-{max(1, int(settings.review_request_hours))} hours') "
                f"  AND o.delivered_at >= datetime('now', '-{days} days') "
                f"GROUP BY o.order_id ORDER BY o.delivered_at DESC LIMIT ?", (lim,)).fetchall()
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": r["order_id"], "user_id": r["user_id"],
                      "order_status": r["status"],
                      "order_status_label": STATUS_LABELS.get(r["status"], r["status"]),
                      "created_at": r["delivered_at"], "items": r["items"] or ""}
                     for r in rows]

        else:  # consulted_no_order
            # NOT EXISTS 故意不按 window 限制买家的历史订单:两个月前买过、昨天来
            # 咨询的人不该被判定成"咨询过没下单"——只要买家名下**任何时候**下过单,
            # 就不算这类商机,窗口只用来限定"咨询"本身的时间范围。
            rows = conn.execute(
                f"SELECT c.user_id, MAX(c.created_at) AS last_at, COUNT(*) AS convs "
                f"FROM conversations c "
                f"WHERE c.created_at >= datetime('now', '-{days} days') "
                f"  AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.user = c.user_id) "
                f"GROUP BY c.user_id ORDER BY convs DESC LIMIT ?", (lim,)).fetchall()
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": "", "user_id": r["user_id"],
                      "conversations": int(r["convs"] or 0), "created_at": r["last_at"]}
                     for r in rows]

        return {"success": True, "kind": kind, "kind_label": OPPORTUNITY_KINDS[kind],
                "window_days": days, "count": len(items), "opportunities": items}
    finally:
        conn.close()


def draft_outreach(user_id: str, content: str, kind: str = "stale_pending_order",
                   order_id: str = "", reason: str = "", offer_note: str = "",
                   coupon_code: str = "") -> dict:
    """为某个商机**起草**一条触达话术,落待审队列(status 恒为 draft)。

    绝不发送,也绝不发券。返回里明确带 status='draft' 与 needs_review_reason,
    让模型无法对店主谎称"已发出"。

    coupon_code(N6)是**建议**:写进草稿 offer.coupon_code,是否真的有这张券、
    要不要发,由人工在审批端点里判断——这里不校验它是否真的存在于店铺的
    券定义(app.agent.tools.order_ops._COUPONS)里,那道校验属于发放本身
    (见 app.agent.coupons.grants.issue_for_draft),不是起草这一步的事;
    起草唯一的写路径是落一行草稿,不做任何会改变"这只是草稿"这个事实的事。
    """
    err = _validate_kind(kind)
    if err is not None:
        return err
    uid = (user_id or "").strip()
    if not uid:
        return {"success": False, "error": "user_id 不能为空"}

    clean, review_reason = _trim_and_classify(content)
    if not clean:
        return {"success": False, "error": "话术内容为空,未生成草稿"}

    offer: dict = {}
    if offer_note:
        offer["note"] = (offer_note or "").strip()
    cc = (coupon_code or "").strip()
    if cc:
        offer["coupon_code"] = cc
    from app.multi_agent import bus

    draft_id = get_db().create_outreach_draft(
        opportunity_type=kind, user_id=uid, order_id=(order_id or "").strip(),
        content=clean, offer=offer, reason=(reason or "").strip(),
        correlation_id=bus.new_correlation_id("DRAFT"), created_by=bus.AGENT_GROWTH,
        needs_review_reason=review_reason)

    return {"success": True, "draft_id": draft_id, "status": "draft",
            "needs_review_reason": review_reason,
            "message": "已生成草稿,需店主在工作台审批后才会发送"}


def list_outreach_drafts_tool(status: str = "draft", limit: int = 20) -> dict:
    """查看触达草稿及其审批状态。只读。"""
    rows = get_db().list_outreach_drafts(status=status or None,
                                         limit=max(1, min(int(limit), 100)))
    return {"success": True, "count": len(rows), "status": status, "drafts": rows}
