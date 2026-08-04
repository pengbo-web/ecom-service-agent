"""确定性异常扫描:用 SQL 聚合 + 可配阈值判定"是否异常",**不调用 LLM**。

为什么不让 LLM 判异常:①同样的数据两次问可能给不同答案,运营无法据此建流程;
②每次扫描都花钱;③阈值调不动,出了误报没法归因。所以判定权归确定性规则,
LLM 只在参谋对话里做"为什么会这样、该怎么办"的解释与建议。

min_samples 是必需的:样本 1 单退 1 单是 100% 退款率,但那不是异常,是没数据。
"""

from __future__ import annotations

from app.agent.tools.shop_analytics import product_diagnostics, service_quality


def _thresholds() -> dict:
    from app.config.settings import settings
    return {
        "refund_rate": float(getattr(settings, "anomaly_refund_rate", 0.15)),
        "tool_error_rate": float(getattr(settings, "anomaly_tool_error_rate", 0.30)),
        "human_rate": float(getattr(settings, "anomaly_human_rate", 0.40)),
        "min_samples": int(getattr(settings, "anomaly_min_samples", 5)),
    }


def anomaly_scan(window_days: int = 7) -> dict:
    """扫描经营与服务异常,返回跨阈值的条目。只读,不发事件,不调 LLM。

    每条异常带 kind / subject / value / threshold / detail,后两者让人和模型都
    能判断"离线多远",而不是只看到一个"异常"标签。
    """
    t = _thresholds()
    anomalies: list[dict] = []

    prod = product_diagnostics(window_days=window_days, top_n=20)
    for p in prod.get("products", []):
        if p["orders"] >= t["min_samples"] and p["refund_rate"] >= t["refund_rate"]:
            top_reason = p["refund_reasons"][0]["reason"] if p["refund_reasons"] else ""
            anomalies.append({
                "kind": "refund_rate_high",
                "subject": p["sku"],
                "subject_name": p["name"],
                "value": p["refund_rate"],
                "threshold": t["refund_rate"],
                "detail": {"orders": p["orders"], "refunds": p["refunds"],
                           "top_reason": top_reason,
                           "refund_reasons": p["refund_reasons"]},
            })

    svc = service_quality(window_days=window_days)
    for s in svc.get("skills", []):
        if s["total"] < t["min_samples"]:
            continue
        if s["tool_error_rate"] >= t["tool_error_rate"]:
            anomalies.append({
                "kind": "tool_error_rate_high",
                "subject": s["skill_name"], "subject_name": s["skill_name"],
                "value": s["tool_error_rate"], "threshold": t["tool_error_rate"],
                "detail": {"total": s["total"], "success_rate": s["success_rate"]},
            })
        if s["human_rate"] >= t["human_rate"]:
            anomalies.append({
                "kind": "human_rate_high",
                "subject": s["skill_name"], "subject_name": s["skill_name"],
                "value": s["human_rate"], "threshold": t["human_rate"],
                "detail": {"total": s["total"]},
            })

    return {"success": True, "window_days": int(window_days),
            "thresholds": t, "anomalies": anomalies}


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
    return {"anomalies": len(anomalies), "published": published,
            "correlation_id": corr}
