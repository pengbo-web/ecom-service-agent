"""确定性异常扫描:用 SQL 聚合 + 可配阈值判定"是否异常",**不调用 LLM**。

为什么不让 LLM 判异常:①同样的数据两次问可能给不同答案,运营无法据此建流程;
②每次扫描都花钱;③阈值调不动,出了误报没法归因。所以判定权归确定性规则,
LLM 只在参谋对话里做"为什么会这样、该怎么办"的解释与建议。

min_samples 是必需的:样本 1 单退 1 单是 100% 退款率,但那不是异常,是没数据。
"""

from __future__ import annotations

from app.agent.tools import reviews as rv
from app.agent.tools import shop_analytics as sa
from app.agent.tools.shop_analytics import (fulfillment_diagnostics,
                                            product_diagnostics,
                                            service_quality)

# product_diagnostics 只返回"退款单数 DESC、下单量 DESC"的前 N 名,这个 N 具名
# 成常量而不是留在调用处的字面量 20——下面 products_truncated 的判断和这次调用
# 用的切片大小必须永远指向同一个数字,两处各写一遍字面量迟早会脱节。
PRODUCT_SCAN_LIMIT = 20

# 差评扫描一次读取的评价行数上限,同一目的:reviews_truncated 的判断与这次调用
# 用的行数上限必须指向同一个数字。
REVIEW_SCAN_LIMIT = rv.REVIEW_SCAN_LIMIT


def _thresholds() -> dict:
    from app.config.settings import settings
    return {
        "refund_rate": float(getattr(settings, "anomaly_refund_rate", 0.15)),
        "tool_error_rate": float(getattr(settings, "anomaly_tool_error_rate", 0.30)),
        "human_rate": float(getattr(settings, "anomaly_human_rate", 0.40)),
        "min_samples": int(getattr(settings, "anomaly_min_samples", 5)),
        "angry_rate": float(getattr(settings, "anomaly_angry_rate", 0.20)),
        "bad_review_rate": float(getattr(settings, "anomaly_bad_review_rate", 0.30)),
        # 履约:已付款却卡在待发货的比例。阈值比退款率宽——积压 25% 已经是
        # 明确的履约问题,而退款率 15% 才算异常,两者的正常基线本来不同。
        "stale_fulfillment_rate": float(
            getattr(settings, "anomaly_stale_fulfillment_rate", 0.25)),
        # 服务健康专用的判定窗口(见 settings.anomaly_service_window_days 那段
        # 注释里的实测教训)。放进 _thresholds 而不是单独取一次:它和上面几条
        # 一样是"判异常的口径",应当跟着 thresholds 一起出现在返回值里,让看到
        # 告警的人能知道这个比率是按多长的窗算的。
        "service_window_days": int(getattr(settings, "anomaly_service_window_days", 1)),
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


def _scanned_review_total(window_days: int) -> int:
    """窗口内 reviews 表实际行数(不受 REVIEW_SCAN_LIMIT 限制),用来判断差评
    扫描是不是把长尾评价切没了。口径同 `_scanned_sku_total`。"""
    conn = sa.get_db().connect()
    try:
        w = sa._window_clause(window_days)
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM reviews WHERE created_at >= {w}"
        ).fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()


def anomaly_scan(window_days: int = 7) -> dict:
    """扫描经营与服务异常,返回跨阈值的条目。只读,不发事件,不调 LLM。

    每条异常带 kind / subject / value / threshold / detail,后两者让人和模型都
    能判断"离线多远",而不是只看到一个"异常"标签。

    **两个窗口,不是一个**:`window_days`(入参,默认 7)只管经营侧——退款率、
    差评率有天然滞后(下单→收货→退款/评价跨若干天),缩窗会把它们压成 0;
    服务健康(工具失败率/转人工率/情绪)走 `anomaly_service_window_days`
    (默认 1 天),因为一个工具是不是坏的只有"现在"这一个时态。把两者混成一个
    7 天窗的后果是实测过的:一个已经修好的工具会被连续报警 7 天,见
    settings.anomaly_service_window_days 那段注释。

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

    # 履约:已付款却卡在待发货的比例。补的是"产出草稿最多的商机类型
    # (stale_pending_order)此前没有任何异常规则会为它发信号"这个盲区——那导致
    # 滞留订单的草稿只能借一条**退款率**诊断当依据,而两者没有因果关系。
    # 滞留口径由 growth 的常量决定,不在这里另定(见 fulfillment_diagnostics)。
    ful = fulfillment_diagnostics(window_days=window_days, top_n=PRODUCT_SCAN_LIMIT)
    for p in ful.get("products", []):
        if (p["orders"] >= t["min_samples"]
                and p["stale_rate"] >= t["stale_fulfillment_rate"]):
            anomalies.append(_finding(
                "fulfillment_delay_high", p["sku"], p["name"],
                p["stale_rate"], t["stale_fulfillment_rate"],
                {"orders": p["orders"], "stale_orders": p["stale_orders"],
                 "stale_after_hours": ful.get("stale_after_hours"),
                 "oldest_pending_at": p["oldest_pending_at"]},
            ))

    products_total = _scanned_sku_total(window_days)

    # 服务健康走**近窗**,不跟经营窗:工具坏没坏只有"现在"这一个时态,而退款率
    # /差评率有天然滞后。两个窗都在返回值里报出来,不让人以为只有一个口径。
    svc_window = t["service_window_days"]
    svc = service_quality(window_days=svc_window)
    near = {s["skill_name"]: s["total"] for s in svc.get("skills", [])}

    # 近窗样本不足 ≠ 健康,所以要如实列出,让"没报警"和"没数据所以报不了警"
    # 成为两件看得见的事(与 products_truncated / reviews_truncated 同姿态)。
    # 这是缩窗的**代价**,摆在返回值里而不是藏起来:一个坏掉之后再没人调用的
    # skill 不会再被报出来。旧的 7 天窗会一直报它——那看起来像"检测更灵敏",
    # 实际是分不清"仍然坏着"和"当时坏过",而这两者的处置完全不同。
    #
    # 关键:**不能只遍历近窗**。service_quality 是 GROUP BY 聚合,窗内没有行的
    # skill 压根不出现,而"坏掉之后零调用"恰恰是近窗 0 行——只遍历 svc 会让最
    # 需要说明的那一种恰好隐身。所以拿经营窗(更宽)的名册当全集。名册仍以
    # window_days 为界:比经营窗还老、且之后零调用的 skill 连名字都取不到,
    # 这是这份列表已知的边界,不是漏报。
    wide = svc if int(window_days) == int(svc_window) else \
        service_quality(window_days=window_days)
    roster = sorted({s["skill_name"] for s in wide.get("skills", [])} | set(near))
    service_insufficient = [
        {"skill_name": n, "total": near.get(n, 0), "min_samples": t["min_samples"]}
        for n in roster if near.get(n, 0) < t["min_samples"]
    ]

    for s in svc.get("skills", []):
        if s["total"] < t["min_samples"]:
            continue                      # 已在 service_insufficient 里报过
        if s["tool_error_rate"] >= t["tool_error_rate"]:
            anomalies.append(_finding(
                "tool_error_rate_high", s["skill_name"], s["skill_name"],
                s["tool_error_rate"], t["tool_error_rate"],
                # window_days 跟着 detail 走:这条告警下游要进参谋的归因 prompt、
                # 要写进共享上下文、还要显示在协作链页面上。一个失败率脱离了
                # 统计窗口就没有意义——同样的 0.84,近 1 天和近 7 天是两件事,
                # 而看到它的人(和模型)没有别的途径知道是哪一个。
                {"total": s["total"], "success_rate": s["success_rate"],
                 "window_days": svc_window},
            ))
        if s["human_rate"] >= t["human_rate"]:
            anomalies.append(_finding(
                "human_rate_high", s["skill_name"], s["skill_name"],
                s["human_rate"], t["human_rate"],
                {"total": s["total"], "window_days": svc_window},
            ))

    # 情绪信号:是否"有情绪问题"由阈值判定,不问模型——判定权归确定性规则,
    # 与其余三类异常同一套口径(跨线才报、min_samples 兜底)。
    emo = svc.get("emotion", {})
    if emo.get("total", 0) >= t["min_samples"] and emo.get("angry_rate", 0.0) >= t["angry_rate"]:
        anomalies.append(_finding(
            "angry_rate_high", "shop", "全店",
            emo["angry_rate"], t["angry_rate"],
            {"total": emo["total"], "counts": emo.get("counts", {}),
             "window_days": svc_window},
        ))

    # 差评率:按商品聚合窗口内评价(rating<=2 为差评),同一套跨线才报 +
    # min_samples 兜底的纪律。db 显式传 sa.get_db()——与本文件其余查询共用
    # 同一份(测试里被 monkeypatch 过的)db 引用,不在 reviews 模块内部另行
    # 解析一次 get_db() 读到不相干的库(见 reviews.py 模块 docstring)。
    review_products = rv.product_review_breakdown(
        sa.get_db(), window_days, limit=REVIEW_SCAN_LIMIT)
    for rp in review_products:
        if rp["total"] >= t["min_samples"] and rp["bad_rate"] >= t["bad_review_rate"]:
            anomalies.append(_finding(
                "bad_review_rate_high", rp["sku"], rp["name"],
                rp["bad_rate"], t["bad_review_rate"],
                {"total": rp["total"], "bad_count": rp["bad_count"],
                 "bad_terms": rp["bad_terms"]},
            ))
    reviews_examined = sum(p["total"] for p in review_products)
    reviews_total = _scanned_review_total(window_days)

    return {"success": True, "window_days": int(window_days),
            # 两个窗都报出来。`window_days` 现在只管经营侧(退款率/差评率),
            # 服务健康那三条走 service_window_days——不报第二个数,调用方会
            # 拿 window_days 去解释一个不是按它算出来的失败率。
            "service_window_days": int(svc_window),
            "service_insufficient": service_insufficient,
            "thresholds": t, "anomalies": anomalies,
            "products_examined": len(products),
            "products_truncated": products_total > len(products),
            "reviews_examined": reviews_examined,
            "reviews_truncated": reviews_total > reviews_examined}


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
        # 不指定收件人:扫描器只宣布"发现了一条异常",谁该处理由总线路由表决定
        # (app/multi_agent/routing.py)。想让新 Agent 也订阅异常信号时,这里
        # 一行都不用改——而这里本就与那个新 Agent 毫无关系。
        if bus.publish(bus.EV_SIGNAL_ANOMALY, a, bus.AGENT_SERVICE,
                       correlation_id=corr):
            published += 1
    # corr 在循环之前就已经生成,不依赖任何一次 publish 是否成功。若总线整体
    # 故障(bus.publish 内部 fail-soft,吞异常后逐条返回 None),这里仍会拿到
    # 一个真实存在、可查询的 correlation_id,但 published == 0——这是"生成了
    # 一条协作链的 id,但链上一个事件都没真正落库"的合理状态,不是 bug。调用
    # 方判断这次扫描是否真的发出了信号,应该看 published 而不是
    # correlation_id 是否非 None。
    return {"anomalies": len(anomalies), "published": published,
            "correlation_id": corr}
