"""营销增长 Agent 的工具:找商机 + **起草**触达话术。

**唯一写路径是 create_outreach_draft(status='draft')**。本模块任何函数都不得
调用消息发送通道——发送只发生在管理端的审批端点里,且必须有人点过"批准"。
这是本项目与"全自动营销"方案的分界:向真实买家发消息是不可逆的对外动作,
必须落在既有的"不可逆动作需人工授权"这条线内。

注入面:商机数据(商品名/退款原因/收货地址)与店主输入都可能含指令性文本。
兜底不是"检出注入",而是**产物形态**——最坏情况也只是一条待审草稿。
承诺类敏感词额外标红,逼人工重点看。
"""

from __future__ import annotations

from app.db import get_db

# 支持的商机类型。未知 kind **拒绝**而不是猜一个,否则模型写错一个词就静默取错人群。
OPPORTUNITY_KINDS = {
    "unpaid_order": "已下单未付款",
    "stalled_bargain": "议价未成交",
    "consulted_no_order": "咨询过但没下单",
}

# 未付款订单的状态取值(库里历史上用过 unpaid / pending_payment 两种写法)
_UNPAID_STATUSES = ("unpaid", "pending_payment", "待支付")


def _commitment_hits(text: str) -> list[str]:
    """命中的金钱承诺词。复用 skill 风险分级的同一份词表,口径统一。"""
    from app.agent.skills.risk import COMMITMENT_KEYWORDS
    return [w for w in COMMITMENT_KEYWORDS if w in (text or "")]


def _sanitize_content(text: str) -> tuple[str, str]:
    """返回 (清洗后的话术, 需人工重点复核的原因)。

    刻意**不删改**命中承诺词的文案:删了店主就看不到 Agent 原本想说什么,
    反而更危险。标红交人工判断,比悄悄改写更诚实。
    """
    clean = (text or "").strip()
    hits = _commitment_hits(clean)
    if hits:
        return clean, "包含金钱承诺词: " + "、".join(hits[:5])
    return clean, ""


def find_opportunities(kind: str = "unpaid_order", window_days: int = 14,
                       limit: int = 20) -> dict:
    """按类型找商机。只读。"""
    if kind not in OPPORTUNITY_KINDS:
        return {"success": False,
                "error": f"未知的 kind「{kind}」,可选: {'、'.join(OPPORTUNITY_KINDS)}"}

    days = max(1, int(window_days))
    lim = max(1, min(int(limit), 100))
    conn = get_db().connect()
    try:
        if kind == "unpaid_order":
            placeholders = ",".join("?" * len(_UNPAID_STATUSES))
            rows = conn.execute(
                f"SELECT o.order_id, o.user AS user_id, o.total, o.created_at, "
                f"       GROUP_CONCAT(oi.name, '、') AS items "
                f"FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.order_id "
                f"WHERE o.status IN ({placeholders}) "
                f"  AND o.created_at >= datetime('now', '-{days} days') "
                f"GROUP BY o.order_id ORDER BY o.created_at DESC LIMIT ?",
                (*_UNPAID_STATUSES, lim)).fetchall()
            items = [{"kind": kind, "order_id": r["order_id"], "user_id": r["user_id"],
                      "amount": float(r["total"] or 0.0), "created_at": r["created_at"],
                      "items": r["items"] or ""} for r in rows]

        elif kind == "stalled_bargain":
            rows = conn.execute(
                f"SELECT b.session_id, b.product_id, b.rounds, b.last_offer, b.updated_at "
                f"FROM bargain_sessions b "
                f"WHERE b.updated_at >= datetime('now', '-{days} days') AND b.rounds > 0 "
                f"ORDER BY b.updated_at DESC LIMIT ?", (lim,)).fetchall()
            items = [{"kind": kind, "order_id": "", "user_id": r["session_id"],
                      "product_id": r["product_id"], "rounds": int(r["rounds"] or 0),
                      "last_offer": r["last_offer"], "created_at": r["updated_at"]}
                     for r in rows]

        else:  # consulted_no_order
            rows = conn.execute(
                f"SELECT c.user_id, MAX(c.created_at) AS last_at, COUNT(*) AS convs "
                f"FROM conversations c "
                f"WHERE c.created_at >= datetime('now', '-{days} days') "
                f"  AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.user = c.user_id "
                f"                  AND o.created_at >= datetime('now', '-{days} days')) "
                f"GROUP BY c.user_id ORDER BY convs DESC LIMIT ?", (lim,)).fetchall()
            items = [{"kind": kind, "order_id": "", "user_id": r["user_id"],
                      "conversations": int(r["convs"] or 0), "created_at": r["last_at"]}
                     for r in rows]

        return {"success": True, "kind": kind, "kind_label": OPPORTUNITY_KINDS[kind],
                "window_days": days, "count": len(items), "opportunities": items}
    finally:
        conn.close()


def draft_outreach(user_id: str, content: str, kind: str = "unpaid_order",
                   order_id: str = "", reason: str = "", offer_note: str = "") -> dict:
    """为某个商机**起草**一条触达话术,落待审队列(status 恒为 draft)。

    绝不发送。返回里明确带 status='draft' 与 needs_review_reason,让模型无法
    对店主谎称"已发出"。
    """
    if kind not in OPPORTUNITY_KINDS:
        return {"success": False,
                "error": f"未知的 kind「{kind}」,可选: {'、'.join(OPPORTUNITY_KINDS)}"}
    uid = (user_id or "").strip()
    if not uid:
        return {"success": False, "error": "user_id 不能为空"}

    clean, review_reason = _sanitize_content(content)
    if not clean:
        return {"success": False, "error": "话术内容为空,未生成草稿"}

    offer = {"note": (offer_note or "").strip()} if offer_note else {}
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
