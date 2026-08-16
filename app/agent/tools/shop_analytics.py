"""店铺参谋 Agent 的只读经营分析工具。

**全只读**:本模块不得出现任何 INSERT/UPDATE/DELETE。参谋的价值是"看清楚",
动手交给客服(对买家)和营销(对商机),职责边界即安全边界。

口径统一:所有窗口用 `created_at >= datetime('now', '-N days')`,与库里
写入时的 `strftime('%Y-%m-%d %H:%M:%S')` 格式可比。除零一律返回 0.0。
"""

from __future__ import annotations

from app.agent.skills.execution_trace import (
    OUTCOME_HANDOFF,
    OUTCOME_SUCCESS,
    OUTCOME_TOOL_ERROR,
)
from app.db import dialect, get_db


def _window_clause(days: int) -> str:
    """窗口下界。此前这里自带一份与 database.py 一模一样的实现——
    时钟表达式必须只有一处,否则换库时一定会漏掉某一份。"""
    return dialect.now_minus(max(1, int(days)), "days")


def _rate(part: int, whole: int) -> float:
    """除零安全的比率。空库/空窗口返回 0.0,不返回 None——调用方是 LLM,
    None 会被渲染成 'null' 让模型编数字。

    注意:这里不做小数位四舍五入——保留原始除法结果,避免舍入误差在
    上游断言(如按分数校验 1/3)时放大成"看似不等"的假阳性。
    """
    return part / whole if whole else 0.0


# 经营统计的事实源,以及它已知不覆盖的部分。
#
# **这不是免责声明,是防止一类具体的错误结论。** 实测撞到过:买家侧实际有 12 笔
# 订单(经 `POST /api/order` 路由到 hmdp),而本模块读的 agent 订单库里只有 2 笔;
# 参谋据此得出过一条诊断——「近 7 天仅 2 笔订单但产生 119 次客服对话,对话量远超
# 订单量」。那个结论是**数据源分裂的产物**,不是经营事实,但它以正常诊断的形式
# 进了协作链、也会进店主的看板。
#
# 两个库的分工见 `app/api/app.py::create_order`:买家有 hmdp token 时订单建到
# hmdp,没有才落本地库;而本模块所有 SQL 都只查本地库,两边没有任何同步。
# 真正的修法(镜像写入 / 改读 hmdp)是一次数据架构决策,单独跟进;在那之前,
# 至少要让**读到这些数字的人和模型知道分母是什么**——把口径塞进返回值里,
# 参谋就不会再把渠道差异当成经营异常。
# 不带 markdown:这一句同时给模型和界面用。加 `**` 强调对模型没有帮助(它不靠
# 星号理解轻重),在控制台上却会原样显示成星号——同一个串两个消费方,就按最朴素的
# 纯文本写。
_DATA_SCOPE = (
    "统计口径:仅覆盖 agent 订单库。买家经 hmdp 渠道下的单不在其中,"
    "两库当前没有同步——因此订单量/GMV 可能显著低于店铺真实成交。"
    "请勿把「对话量远超订单量」这类比例失衡当成经营异常,它更可能是渠道口径差异。"
)

#: 服务质量只统计**真实买家流量**。
#:
#: **实测踩过一次,而且直接骗到了店主。** 走查时参谋在日报里写:
#:
#:     track-order 技能工具失败率:当前值 100%,告警线 30%(超出 70%)
#:     建议:立即核查 track-order 技能配置
#:
#: 查那 13 次失败的来源:**13/13 全是 `dev`** —— 我自己走查时反复查同一个不属于
#: 当前用户的订单号,归属校验按设计拒绝。真实买家 0 条。也就是说参谋拿着一批
#: 压测/走查数据,让店主去修一份一个字都没写错的技能。
#:
#: 看门狗那一侧(`skill_watchdog.check_canaries`)早就按 live 过滤了,这一侧漏了
#: ——同一批脏数据,一条路挡住、孪生路没挡。告警比自动回滚更需要挡:回滚至少
#: 还有可信下限兜着,而一条假警报是直接送到人眼前的。
_TRAFFIC_SCOPE = (
    "服务质量口径:只统计真实买家流量(source=live),已排除压测/离线评测/人工走查"
    "产生的轨迹。若某 skill 的 total 为 0,是这个窗口内没有真实买家用到它,"
    "**不是它不工作**——不要据此判定异常或建议整改。"
)


def shop_overview(window_days: int = 7) -> dict:
    """店铺经营总览:订单量 / GMV / 客单价 / 退款率 / 取消率 / 咨询会话数。

    返回值带 `data_scope`(见 `_DATA_SCOPE`):这些数字只覆盖 agent 订单库,
    不含 hmdp 渠道的成交。这一句是给**读结果的模型和人**看的,不是文档注释——
    参谋 Agent 只能看到工具返回的 JSON,口径不在里面它就无从得知。
    """
    conn = get_db().connect()
    try:
        w = _window_clause(window_days)
        row = conn.execute(
            f"SELECT COUNT(*) AS orders, COALESCE(SUM(total),0) AS gmv, "
            f"SUM(CASE WHEN refund_status IS NOT NULL AND refund_status != '' "
            f"     THEN 1 ELSE 0 END) AS refunds, "
            f"SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancels "
            f"FROM orders WHERE created_at >= {w}").fetchone()
        convs = conn.execute(
            f"SELECT COUNT(*) AS c FROM conversations WHERE created_at >= {w}"
        ).fetchone()["c"]
        orders = int(row["orders"] or 0)
        gmv = float(row["gmv"] or 0.0)
        return {
            "success": True,
            "window_days": int(window_days),
            "orders": orders,
            "gmv": round(gmv, 2),
            "avg_order_value": round(gmv / orders, 2) if orders else 0.0,
            "refunds": int(row["refunds"] or 0),
            "refund_rate": _rate(int(row["refunds"] or 0), orders),
            "cancels": int(row["cancels"] or 0),
            "cancel_rate": _rate(int(row["cancels"] or 0), orders),
            "conversations": int(convs or 0),
            "orders_per_conversation": _rate(orders, int(convs or 0)),
            "data_scope": _DATA_SCOPE,
        }
    finally:
        conn.close()


def product_diagnostics(window_days: int = 7, top_n: int = 5) -> dict:
    """按商品的诊断:下单量 / 销售额 / 退款率 / 退款原因 top3 / 当前库存。

    按"退款单数 DESC, 下单量 DESC"排序——参谋要先看最疼的商品,不是卖最好的。

    近似口径(重要,读结果的人和 LLM 都要知道):退款记录在**订单**级别,
    但这里按 `order_items ⋈ orders` 以 sku 分组统计。一笔订单只要退款,
    就会把这笔退款计入该订单**每一个** SKU 名下。因此对多商品订单,
    单个商品的 `refund_rate` 是被高估的上界(upper bound),不是精确的
    "这个商品自己导致退款"的比例——多件合并下单越常见,偏差越大。
    """
    conn = get_db().connect()
    try:
        w = _window_clause(window_days)
        rows = conn.execute(
            f"SELECT oi.sku AS sku, MAX(oi.name) AS name, "
            f"       COUNT(DISTINCT o.order_id) AS orders, "
            f"       COALESCE(SUM(oi.price * oi.quantity),0) AS revenue, "
            f"       SUM(CASE WHEN o.refund_status IS NOT NULL AND o.refund_status != '' "
            f"            THEN 1 ELSE 0 END) AS refunds "
            f"FROM order_items oi JOIN orders o ON o.order_id = oi.order_id "
            f"WHERE o.created_at >= {w} AND oi.sku IS NOT NULL AND oi.sku != '' "
            f"GROUP BY oi.sku ORDER BY refunds DESC, orders DESC LIMIT ?",
            (max(1, int(top_n)),)).fetchall()

        products = []
        for r in rows:
            reasons = conn.execute(
                f"SELECT o.refund_reason AS reason, COUNT(*) AS n "
                f"FROM orders o JOIN order_items oi ON o.order_id = oi.order_id "
                f"WHERE oi.sku = ? AND o.created_at >= {w} "
                f"  AND o.refund_reason IS NOT NULL AND o.refund_reason != '' "
                f"GROUP BY o.refund_reason ORDER BY n DESC LIMIT 3",
                (r["sku"],)).fetchall()
            stock_row = conn.execute(
                "SELECT stock FROM products WHERE product_id = ?", (r["sku"],)).fetchone()
            products.append({
                "sku": r["sku"],
                "name": r["name"],
                "orders": int(r["orders"] or 0),
                "revenue": round(float(r["revenue"] or 0.0), 2),
                "refunds": int(r["refunds"] or 0),
                "refund_rate": _rate(int(r["refunds"] or 0), int(r["orders"] or 0)),
                "stock": int(stock_row["stock"]) if stock_row else None,
                "refund_reasons": [{"reason": x["reason"], "count": int(x["n"])}
                                   for x in reasons],
            })
        return {"success": True, "window_days": int(window_days), "products": products}
    finally:
        conn.close()


def service_quality(window_days: int = 7) -> dict:
    """服务质量:按 skill 的执行成功率 / 工具失败率 / 转人工率。

    数据来自 skill_traces(自进化体系已在记的执行轨迹),不额外埋点。

    三个分桶的字面量不在 SQL 里写死——绑定为查询参数,直接取自
    `app.agent.skills.execution_trace` 里 `SkillTurn.outcome()` 实际写出的
    OUTCOME_* 常量,防止两边字面量各写各的、慢慢漂移出一个永远不命中的分支
    (曾经的教训:`human_rate` 用字面量 `'requires_human'`,但落库写的是
    `OUTCOME_HANDOFF = 'handoff'`,导致转人工率永远汇报成 0.0)。

    `other`:outcome 不属于以上三种已知取值的行数(如历史脏数据、未来新增
    的 outcome 取值)。这些行仍计入 `total`,但不落进任何一个 rate 的分子,
    所以三个 rate 不保证求和为 1——这里选择显式给出 `other` 计数,而不是只
    在文档里提一句,因为这个模块的下游是"按阈值判异常"的告警:如果只在
    文档里说明,未来新增/拼错的 outcome 会悄悄从三个 rate 里消失、不体现
    在任何数字上,和"这个 skill 一直很健康"长得一模一样,等于把异常藏起来
    了;`other` 让这种情况在返回值里可见,便于告警侧决定要不要单独关注。

    `emotion`(N2):按每一轮统计的情绪分布(neutral/unhappy/angry 计数 + 激烈
    占比),来自 `turn_signals`(与 skill_traces 分表——skill_traces 只在加载
    过 skill 时才有行,当分母会失真)。是否"有情绪问题"由 anomaly.py 按阈值
    判定,本方法只负责把统计口径摆出来,不下结论。
    """
    from app.agent.runtime_context import DECISION_SOURCES

    sources = sorted(DECISION_SOURCES)
    conn = get_db().connect()
    try:
        w = _window_clause(window_days)
        # COALESCE:`source` 列是后加的,历史行回填成 'unknown';但别的写入路径仍
        # 可能留下 NULL,而 NULL 不等于任何值、会被 IN 静默漏掉 —— 那些行既不算
        # live 也不算 unknown,凭空从统计里消失(与 database.skill_trace_counts
        # 同一条口径)。
        rows = conn.execute(
            f"SELECT skill_name, COUNT(*) AS total, "
            f"  SUM(CASE WHEN outcome = ? THEN 1 ELSE 0 END) AS ok, "
            f"  SUM(CASE WHEN outcome = ? THEN 1 ELSE 0 END) AS tool_error, "
            f"  SUM(CASE WHEN outcome = ? THEN 1 ELSE 0 END) AS human "
            f"FROM skill_traces WHERE created_at >= {w} "
            f"  AND COALESCE(source, 'unknown') IN ({','.join('?' * len(sources))}) "
            f"GROUP BY skill_name ORDER BY total DESC",
            (OUTCOME_SUCCESS, OUTCOME_TOOL_ERROR, OUTCOME_HANDOFF, *sources)).fetchall()
        skills = []
        for r in rows:
            total = int(r["total"] or 0)
            ok = int(r["ok"] or 0)
            tool_error = int(r["tool_error"] or 0)
            human = int(r["human"] or 0)
            skills.append({
                "skill_name": r["skill_name"],
                "total": total,
                "success_rate": _rate(ok, total),
                "tool_error_rate": _rate(tool_error, total),
                "human_rate": _rate(human, total),
                "other": total - ok - tool_error - human,
            })
        emotion = get_db().emotion_distribution(window_days=window_days)
        # traffic_scope 必须进返回值,不能只写在 docstring 里:参谋 Agent 只看得到
        # 工具返回的 JSON,口径不在里面它就无从得知(与 shop_overview 的
        # data_scope 同一条理由)。
        return {"success": True, "window_days": int(window_days), "skills": skills,
                "emotion": emotion, "traffic_scope": _TRAFFIC_SCOPE}
    finally:
        conn.close()
