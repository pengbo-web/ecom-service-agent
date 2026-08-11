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
from app.db import dialect, get_db

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



def _split_skus(raw) -> list[str]:
    """把 GROUP_CONCAT 出来的 sku 串(或单个 sku)切成去重列表,保持出现顺序。

    下游用它判断"这条商机涉不涉及某个 SKU"——一条关于商品 A 的诊断不该被拿去
    给商品 B 的买家起草(见 app/multi_agent/collab.py 的 `_diagnosis_applies_to`)。
    LEFT JOIN 拿不到行时是 None,返回空列表:**空 = 不知道涉及哪些 SKU**,
    与"确定不涉及"是两件事,由调用方决定怎么处置。
    """
    if not raw:
        return []
    seen: dict[str, None] = {}
    for part in str(raw).split(","):
        sku = part.strip()
        if sku:
            seen.setdefault(sku, None)
    return list(seen)


def _stale_hours(col: str) -> str:
    """算"这条商机已经滞留了多少小时"的 SQL 片段。

    **必须在 SQL 里算,不能在 Python 里算。** 本项目 `Database._now()` 写的是
    本地时间,而下面每条 WHERE 用的是 SQLite 的 `datetime('now')`(UTC);在
    Python 侧用 datetime.now() 再算一次时间差,会与"筛出这一行的那个条件"用
    两个不同的时钟——一条刚好卡在阈值边缘的订单会出现"WHERE 认为已滞留 25h、
    打分认为滞留 -7h"这种自相矛盾。用同一个 `now` 就没有这个缝。
    """
    return dialect.elapsed_hours(col)


def _fetch_limit(lim: int) -> int:
    """打分排序时的**候选池**大小。

    不能只对 LIMIT 之后剩下的那几条排序:下面每条 SQL 都是 `ORDER BY <时间>
    DESC`,取到的恰恰是**最新**的一批——而"最新"意味着滞留最短,正是最不该
    优先催的那些。真正该排在最前面的老单本来就在 LIMIT 之外,再怎么排也排不
    出来。所以先多取一些进池子,打完分再截断到调用方要的 limit。
    """
    if not settings.opportunity_priority_enabled:
        return lim
    return min(max(lim, lim * max(1, int(settings.priority_overfetch_factor))),
               max(lim, int(settings.priority_overfetch_max)))


def _rank(items: list[dict], lim: int) -> list[dict]:
    """打分排序并截断到 lim。fail-soft:打分链路任何异常都退回原顺序原条数——
    排序是锦上添花,绝不能让"找商机"这件事本身失败。"""
    if not settings.opportunity_priority_enabled:
        return items[:lim]
    try:
        from app.agent.tools.priority import conversion_rates, rank
        return rank(items, conversion_rates(get_db()))[:lim]
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("商机优先级打分失败,退回时间倒序",
                                            exc_info=True)
        return items[:lim]


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
    pool = _fetch_limit(lim)   # 打分要在候选池里排,不是排 LIMIT 之后的残余(见 _fetch_limit)
    conn = get_db().connect()
    try:
        if kind == "stale_pending_order":
            rows = conn.execute(
                f"SELECT o.order_id, o.user AS user_id, o.status, o.total, o.created_at, "
                f"       {_stale_hours('o.created_at')} AS stale_hours, "
                f"       GROUP_CONCAT(oi.name, '、') AS items, "
                # skus:下游要判断"这条商机涉不涉及某个 SKU"。SQL 本来就 join 了
                # order_items,只是没把 sku 带出来——与 stale_hours 当初漏掉是
                # 同一类"算了但没传下去"。没有它,一条关于商品 A 的诊断会被拿去
                # 给商品 B 的买家起草,并把 A 的结论当成 B 的事实说出去。
                f"       GROUP_CONCAT(oi.sku, ',') AS skus "
                f"FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.order_id "
                f"WHERE o.status = ? "
                f"  AND o.created_at <= {dialect.now_minus(_STALE_PENDING_HOURS, 'hours')} "
                f"  AND o.created_at >= {dialect.now_minus(days, 'days')} "
                f"GROUP BY o.order_id ORDER BY o.created_at DESC LIMIT ?",
                (_STALE_PENDING_STATUS, pool)).fetchall()
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
                      # SQL 算了 stale_hours,这里必须带过去。漏掉不会报错,只会让
                      # priority.score_opportunity 取不到值、静默 fallback 到 0.0
                      # ——滞留时长占权重 0.4,归零后这一类商机等于**只按金额排序**,
                      # 而"把拖了一周的大额单排到前面"正是这个模块存在的全部理由。
                      # 实测症状:一笔 08-02 下的 pending 单,理由栏写着"滞留 0h",
                      # 分数 0.45(= 0.4×0 + 0.3×金额封顶 + 0.3×转化先验),而带上
                      # 真实的 ~216h 应该是 0.85。七个分支里只有这一个漏了,而它
                      # 恰好是默认 kind、也是唯一有真实数据的那个。
                      "stale_hours": r["stale_hours"],
                      "skus": _split_skus(r["skus"]),
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
                f"       b.updated_at, c.user_id AS buyer_id, "
                f"       {_stale_hours('b.updated_at')} AS stale_hours "
                f"FROM bargain_sessions b "
                f"JOIN conversations c ON c.conversation_id = b.session_id "
                f"WHERE b.updated_at >= {dialect.now_minus(days, 'days')} AND b.rounds > 0 "
                f"ORDER BY b.updated_at DESC LIMIT ?", (pool,)).fetchall()
            # last_offer 是买家最后出的价:它就是这条商机的"金额",拿来打分比
            # 按"金额未知"走中性值更准(议价谈崩的那单值多少钱,库里其实有)。
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": "", "user_id": r["buyer_id"],
                      "session_id": r["session_id"], "product_id": r["product_id"],
                      "rounds": int(r["rounds"] or 0), "last_offer": r["last_offer"],
                      "amount": float(r["last_offer"] or 0.0),
                      "stale_hours": r["stale_hours"],
                      "created_at": r["updated_at"]} for r in rows]

        elif kind == "unpaid_order":
            # 催付款:status='unpaid' 且超过 settings.unpaid_stale_hours——刚下单
            # 还没到催的时候(买家可能就在结账流程里),阈值以内一律不算商机。
            # order_status(_label) 同样复用 STATUS_LABELS 这份唯一口径,让起草
            # 模型据此判断买家真实进度是"没付钱",不是"已付款等发货"。
            rows = conn.execute(
                f"SELECT o.order_id, o.user AS user_id, o.status, o.total, o.created_at, "
                f"       {_stale_hours('o.created_at')} AS stale_hours, "
                f"       GROUP_CONCAT(oi.name, '、') AS items, "
                f"       GROUP_CONCAT(oi.sku, ',') AS skus "
                f"FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.order_id "
                f"WHERE o.status = 'unpaid' "
                f"  AND o.created_at <= {dialect.now_minus(max(1, int(settings.unpaid_stale_hours)), 'hours')} "
                f"  AND o.created_at >= {dialect.now_minus(days, 'days')} "
                f"GROUP BY o.order_id ORDER BY o.created_at DESC LIMIT ?", (pool,)).fetchall()
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": r["order_id"], "user_id": r["user_id"],
                      "order_status": r["status"],
                      "order_status_label": STATUS_LABELS.get(r["status"], r["status"]),
                      "amount": float(r["total"] or 0.0), "created_at": r["created_at"],
                      "stale_hours": r["stale_hours"],
                      "skus": _split_skus(r["skus"]),
                      "items": r["items"] or ""} for r in rows]

        elif kind == "abandoned_cart":
            # 弃单挽回:carts.status='active' 且 added_at 超过 settings.cart_stale_hours。
            # 数据来自真实的 carts 表(N5 新增),不再是"永远查不到一行"的占位符。
            rows = conn.execute(
                # LEFT JOIN products 只为拿单价算这一车的金额:购物车金额库里
                # 本来就有(carts.sku 就是 products.product_id,见 cart.add_to_cart),
                # 之前没取,导致弃单商机在打分时一律按"金额未知"走中性值——同样
                # 是加购,一车 2000 块和一车 29 块被排成一样,是白丢的信息。
                # LEFT JOIN 而不是 JOIN:hmdp 等外部来源的 sku 在本地 products
                # 里可能没有对应行,那种情况宁可金额为空(退回中性值),也不能
                # 把这条弃单商机整条丢掉。
                f"SELECT c.user_id, c.sku, c.quantity, c.added_at, "
                f"       {_stale_hours('c.added_at')} AS stale_hours, "
                f"       (p.price * c.quantity) AS cart_amount "
                f"FROM carts c LEFT JOIN products p ON p.product_id = c.sku "
                f"WHERE c.status = 'active' "
                f"  AND c.added_at <= {dialect.now_minus(max(1, int(settings.cart_stale_hours)), 'hours')} "
                f"  AND c.added_at >= {dialect.now_minus(days, 'days')} "
                f"ORDER BY c.added_at DESC LIMIT ?", (pool,)).fetchall()
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": "", "user_id": r["user_id"], "sku": r["sku"],
                      "quantity": int(r["quantity"] or 0), "created_at": r["added_at"],
                      "amount": float(r["cart_amount"] or 0.0),
                      # skus 与上面两类同名:下游判相关性只认一个键,不必按 kind 分叉
                      "skus": _split_skus(r["sku"]),
                      "stale_hours": r["stale_hours"]}
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
                f"       {_stale_hours('o.shipped_at')} AS stale_hours, "
                f"       GROUP_CONCAT(oi.name, '、') AS items "
                f"FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.order_id "
                f"WHERE o.status = 'shipped' "
                f"  AND o.shipped_at <= {dialect.now_minus(max(1, int(settings.shipped_care_hours)), 'hours')} "
                f"  AND o.shipped_at >= {dialect.now_minus(days, 'days')} "
                f"GROUP BY o.order_id ORDER BY o.shipped_at DESC LIMIT ?", (pool,)).fetchall()
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": r["order_id"], "user_id": r["user_id"],
                      "order_status": r["status"],
                      "order_status_label": STATUS_LABELS.get(r["status"], r["status"]),
                      "tracking_number": r["tracking_number"] or "",
                      "carrier": r["carrier"] or "",
                      "estimated_delivery": r["estimated_delivery"] or "",
                      "amount": float(r["total"] or 0.0), "created_at": r["shipped_at"],
                      "stale_hours": r["stale_hours"],
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
                f"SELECT o.order_id, o.user AS user_id, o.status, o.delivered_at, o.total, "
                f"       {_stale_hours('o.delivered_at')} AS stale_hours, "
                f"       GROUP_CONCAT(oi.name, '、') AS items "
                f"FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
                f"WHERE o.status = 'delivered' "
                f"  AND NOT EXISTS (SELECT 1 FROM reviews r "
                f"                  WHERE r.order_id = o.order_id AND r.sku = oi.sku) "
                f"  AND o.delivered_at <= {dialect.now_minus(max(1, int(settings.review_request_hours)), 'hours')} "
                f"  AND o.delivered_at >= {dialect.now_minus(days, 'days')} "
                f"GROUP BY o.order_id ORDER BY o.delivered_at DESC LIMIT ?", (pool,)).fetchall()
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": r["order_id"], "user_id": r["user_id"],
                      "order_status": r["status"],
                      "order_status_label": STATUS_LABELS.get(r["status"], r["status"]),
                      "amount": float(r["total"] or 0.0),
                      "stale_hours": r["stale_hours"],
                      "created_at": r["delivered_at"], "items": r["items"] or ""}
                     for r in rows]

        else:  # consulted_no_order
            # NOT EXISTS 故意不按 window 限制买家的历史订单:两个月前买过、昨天来
            # 咨询的人不该被判定成"咨询过没下单"——只要买家名下**任何时候**下过单,
            # 就不算这类商机,窗口只用来限定"咨询"本身的时间范围。
            rows = conn.execute(
                f"SELECT c.user_id, MAX(c.created_at) AS last_at, COUNT(*) AS convs, "
                f"       {_stale_hours('MAX(c.created_at)')} AS stale_hours "
                f"FROM conversations c "
                f"WHERE c.created_at >= {dialect.now_minus(days, 'days')} "
                f"  AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.user = c.user_id) "
                f"GROUP BY c.user_id ORDER BY convs DESC LIMIT ?", (pool,)).fetchall()
            # 这一类**没有**订单金额可用(买家压根没下过单),打分时按
            # priority_unknown_amount 走中性值——见 priority.score_opportunity
            # 里那段"把缺失当成最差是排序里最常见的一类偏见"。
            items = [{"kind": kind, "situation_label": OPPORTUNITY_KINDS[kind],
                      "order_id": "", "user_id": r["user_id"],
                      "conversations": int(r["convs"] or 0), "created_at": r["last_at"],
                      "stale_hours": r["stale_hours"]}
                     for r in rows]

    finally:
        conn.close()

    # 打分排序放在 conn 关闭之后:_rank 内部要另开连接读历史转化率
    # (conversion_rates),嵌在这个 try 里会在同一个函数里持有两条连接。
    ranked = _rank(items, lim)
    return {"success": True, "kind": kind, "kind_label": OPPORTUNITY_KINDS[kind],
            "window_days": days, "count": len(ranked),
            "ranked_by": ("priority" if settings.opportunity_priority_enabled
                          else "recency"),
            "opportunities": ranked}


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

    if draft_id is None:
        # 数据层唯一约束拒绝:这个买家的这条商机已经躺着一条待审草稿。
        # 给模型一句**能读懂、不会诱导它重试**的话——它看到 success=False 的
        # 第一反应是换个措辞再调一次,所以这里必须明说"不用重试"。
        return {"success": False,
                "error": f"买家 {uid} 的「{OPPORTUNITY_KINDS[kind]}」已有一条待审草稿,"
                         f"无需重复起草(重复排队只会稀释店主的审批注意力)。"
                         f"换个措辞重试也会被同样拒绝——请改去处理别的商机。"}

    return {"success": True, "draft_id": draft_id, "status": "draft",
            "needs_review_reason": review_reason,
            "message": "已生成草稿,需店主在工作台审批后才会发送"}


def list_outreach_drafts_tool(status: str = "draft", limit: int = 20) -> dict:
    """查看触达草稿及其审批状态。只读。"""
    rows = get_db().list_outreach_drafts(status=status or None,
                                         limit=max(1, min(int(limit), 100)))
    return {"success": True, "count": len(rows), "status": status, "drafts": rows}
