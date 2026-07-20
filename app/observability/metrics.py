"""基于 TraceStore 的指标聚合。"""

from app.config.settings import settings


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[k]


def compute_metrics(store) -> dict:
    traces = store.all_traces()
    spans = store.all_spans()

    total = len(traces)
    errors = sum(1 for t in traces if t["status"] == "error")
    latencies = [t["latency_ms"] for t in traces]
    prompt_tokens = sum(t["prompt_tokens"] or 0 for t in traces)
    completion_tokens = sum(t["completion_tokens"] or 0 for t in traces)

    tool_spans = [s for s in spans if s["kind"] == "tool"]
    tool_ok = sum(1 for s in tool_spans if s["success"] == 1)

    intent_dist: dict = {}
    for t in traces:
        key = t["intent"] or "unknown"
        intent_dist[key] = intent_dist.get(key, 0) + 1

    est_cost = (prompt_tokens / 1000.0) * settings.price_per_1k_prompt + \
               (completion_tokens / 1000.0) * settings.price_per_1k_completion

    return {
        "total_traces": total,
        "error_rate": (errors / total) if total else 0.0,
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "total_prompt_tokens": prompt_tokens,
        "total_completion_tokens": completion_tokens,
        "est_cost_usd": round(est_cost, 4),
        "tool_calls": len(tool_spans),
        "tool_success_rate": (tool_ok / len(tool_spans)) if tool_spans else 0.0,
        "intent_distribution": intent_dist,
    }
