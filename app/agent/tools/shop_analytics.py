"""店铺参谋 Agent 的只读经营分析工具。

**全只读**:本模块不得出现任何 INSERT/UPDATE/DELETE。参谋的价值是"看清楚",
动手交给客服(对买家)和营销(对商机),职责边界即安全边界。

口径统一:所有窗口用 `created_at >= datetime('now', '-N days')`,与库里
写入时的 `strftime('%Y-%m-%d %H:%M:%S')` 格式可比。除零一律返回 0.0。
"""

from __future__ import annotations

from app.db import get_db


def _window_clause(days: int) -> str:
    return f"datetime('now', '-{max(1, int(days))} days')"


def _rate(part: int, whole: int) -> float:
    """除零安全的比率。空库/空窗口返回 0.0,不返回 None——调用方是 LLM,
    None 会被渲染成 'null' 让模型编数字。

    注意:这里不做小数位四舍五入——保留原始除法结果,避免舍入误差在
    上游断言(如按分数校验 1/3)时放大成"看似不等"的假阳性。
    """
    return part / whole if whole else 0.0


def shop_overview(window_days: int = 7) -> dict:
    """店铺经营总览:订单量 / GMV / 客单价 / 退款率 / 取消率 / 咨询会话数。"""
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
        }
    finally:
        conn.close()


def product_diagnostics(window_days: int = 7, top_n: int = 5) -> dict:
    """按商品的诊断:下单量 / 销售额 / 退款率 / 退款原因 top3 / 当前库存。

    按"退款单数 DESC, 下单量 DESC"排序——参谋要先看最疼的商品,不是卖最好的。
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
    """
    conn = get_db().connect()
    try:
        w = _window_clause(window_days)
        rows = conn.execute(
            f"SELECT skill_name, COUNT(*) AS total, "
            f"  SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS ok, "
            f"  SUM(CASE WHEN outcome = 'tool_error' THEN 1 ELSE 0 END) AS tool_error, "
            f"  SUM(CASE WHEN outcome = 'requires_human' THEN 1 ELSE 0 END) AS human "
            f"FROM skill_traces WHERE created_at >= {w} "
            f"GROUP BY skill_name ORDER BY total DESC").fetchall()
        skills = []
        for r in rows:
            total = int(r["total"] or 0)
            skills.append({
                "skill_name": r["skill_name"],
                "total": total,
                "success_rate": _rate(int(r["ok"] or 0), total),
                "tool_error_rate": _rate(int(r["tool_error"] or 0), total),
                "human_rate": _rate(int(r["human"] or 0), total),
            })
        return {"success": True, "window_days": int(window_days), "skills": skills}
    finally:
        conn.close()
