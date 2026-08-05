"""确定性异常扫描:用 SQL 聚合 + 可配阈值判定"是否异常",**不调用 LLM**。

为什么不让 LLM 判异常:①同样的数据两次问可能给不同答案,运营无法据此建流程;
②每次扫描都花钱;③阈值调不动,出了误报没法归因。所以判定权归确定性规则,
LLM 只在参谋对话里做"为什么会这样、该怎么办"的解释与建议。

min_samples 是必需的:样本 1 单退 1 单是 100% 退款率,但那不是异常,是没数据。
"""

from __future__ import annotations

from app.agent.tools import shop_analytics as sa
from app.agent.tools.shop_analytics import product_diagnostics, service_quality

# product_diagnostics 只返回"退款单数 DESC、下单量 DESC"的前 N 名,这个 N 具名
# 成常量而不是留在调用处的字面量 20——下面 products_truncated 的判断和这次调用
# 用的切片大小必须永远指向同一个数字,两处各写一遍字面量迟早会脱节。
PRODUCT_SCAN_LIMIT = 20


def _thresholds() -> dict:
    from app.config.settings import settings
    return {
        "refund_rate": float(getattr(settings, "anomaly_refund_rate", 0.15)),
        "tool_error_rate": float(getattr(settings, "anomaly_tool_error_rate", 0.30)),
        "human_rate": float(getattr(settings, "anomaly_human_rate", 0.40)),
        "min_samples": int(getattr(settings, "anomaly_min_samples", 5)),
        "angry_rate": float(getattr(settings, "anomaly_angry_rate", 0.20)),
    }


def _finding(kind: str, subject: str, subject_name: str, value: float,
             threshold: float, detail: dict) -> dict:
    """三类异常共用同一个"发现"形状(kind/subject/subject_name/value/threshold/
    detail)。这个形状本身是下游任务的既定契约,没有理由让三处构造字典的代码
    各写一遍、慢慢在键名上漂移出不一致。
    """
    return {"kind": kind, "subject": subject, "subject_name": subject_name,
            "value": value, "threshold": threshold, "detail": detail}


def _scanned_sku_total(window_days: int) -> int:
    """窗口内实际有下单记录的 SKU 总数(不受 PRODUCT_SCAN_LIMIT 限制),用来
    判断 product_diagnostics 的切片是不是把长尾商品切没了。

    SQL 口径刻意与 shop_analytics.product_diagnostics 主查询的 JOIN/WHERE
    保持一致——否则这里数出来的"总数"和切片里的"examined"口径对不上,
    products_truncated 的判断就文不对题。
    """
    conn = sa.get_db().connect()
    try:
        w = sa._window_clause(window_days)
        row = conn.execute(
            f"SELECT COUNT(DISTINCT oi.sku) AS n "
            f"FROM order_items oi JOIN orders o ON o.order_id = oi.order_id "
            f"WHERE o.created_at >= {w} AND oi.sku IS NOT NULL AND oi.sku != ''"
        ).fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()


def anomaly_scan(window_days: int = 7) -> dict:
    """扫描经营与服务异常,返回跨阈值的条目。只读,不发事件,不调 LLM。

    每条异常带 kind / subject / value / threshold / detail,后两者让人和模型都
    能判断"离线多远",而不是只看到一个"异常"标签。

    **盲区提示(重要)**:商品侧只看 product_diagnostics 给的前
    PRODUCT_SCAN_LIMIT 名,而它按"退款单数 DESC、下单量 DESC"排序——排的是
    **绝对退款单数**,不是退款率。一旦店里 SKU 数超过这个切片大小,一个绝对
    退款数排在切片外、但退款**率**是全店最高的低量商品,永远进不了下面的
    阈值判断,也就永远不会出现在 anomalies 里。返回值里的 products_examined /
    products_truncated 就是为了让这个盲区在数字上可见,不让"扫过一遍"看起来
    像"全店都看过了"。
    """
    t = _thresholds()
    anomalies: list[dict] = []

    prod = product_diagnostics(window_days=window_days, top_n=PRODUCT_SCAN_LIMIT)
    products = prod.get("products", [])
    for p in products:
        if p["orders"] >= t["min_samples"] and p["refund_rate"] >= t["refund_rate"]:
            top_reason = p["refund_reasons"][0]["reason"] if p["refund_reasons"] else ""
            anomalies.append(_finding(
                "refund_rate_high", p["sku"], p["name"],
                p["refund_rate"], t["refund_rate"],
                {"orders": p["orders"], "refunds": p["refunds"],
                 "top_reason": top_reason, "refund_reasons": p["refund_reasons"]},
            ))

    products_total = _scanned_sku_total(window_days)

    svc = service_quality(window_days=window_days)
    for s in svc.get("skills", []):
        if s["total"] < t["min_samples"]:
            continue
        if s["tool_error_rate"] >= t["tool_error_rate"]:
            anomalies.append(_finding(
                "tool_error_rate_high", s["skill_name"], s["skill_name"],
                s["tool_error_rate"], t["tool_error_rate"],
                {"total": s["total"], "success_rate": s["success_rate"]},
            ))
        if s["human_rate"] >= t["human_rate"]:
            anomalies.append(_finding(
                "human_rate_high", s["skill_name"], s["skill_name"],
                s["human_rate"], t["human_rate"],
                {"total": s["total"]},
            ))

    # 情绪信号:是否"有情绪问题"由阈值判定,不问模型——判定权归确定性规则,
    # 与其余三类异常同一套口径(跨线才报、min_samples 兜底)。
    emo = svc.get("emotion", {})
    if emo.get("total", 0) >= t["min_samples"] and emo.get("angry_rate", 0.0) >= t["angry_rate"]:
        anomalies.append(_finding(
            "angry_rate_high", "shop", "全店",
            emo["angry_rate"], t["angry_rate"],
            {"total": emo["total"], "counts": emo.get("counts", {})},
        ))

    return {"success": True, "window_days": int(window_days),
            "thresholds": t, "anomalies": anomalies,
            "products_examined": len(products),
            "products_truncated": products_total > len(products)}


def scan_and_publish(window_days: int = 7) -> dict:
    """扫描并把每条异常作为 signal.anomaly 发给参谋 Agent。

    同一次扫描共用一个 correlation_id:一次扫描出的多条异常属于同一次"体检",
    时间线上应当聚在一起看。
    """
    from app.multi_agent import bus

    result = anomaly_scan(window_days=window_days)
    anomalies = result["anomalies"]
    if not anomalies:
        return {"anomalies": 0, "published": 0, "correlation_id": None}

    corr = bus.new_correlation_id("SCAN")
    published = 0
    for a in anomalies:
        if bus.publish(bus.EV_SIGNAL_ANOMALY, a, bus.AGENT_SERVICE,
                       bus.AGENT_ANALYST, correlation_id=corr):
            published += 1
    # corr 在循环之前就已经生成,不依赖任何一次 publish 是否成功。若总线整体
    # 故障(bus.publish 内部 fail-soft,吞异常后逐条返回 None),这里仍会拿到
    # 一个真实存在、可查询的 correlation_id,但 published == 0——这是"生成了
    # 一条协作链的 id,但链上一个事件都没真正落库"的合理状态,不是 bug。调用
    # 方判断这次扫描是否真的发出了信号,应该看 published 而不是
    # correlation_id 是否非 None。
    return {"anomalies": len(anomalies), "published": published,
            "correlation_id": corr}
