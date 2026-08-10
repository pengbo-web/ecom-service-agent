"""基于 TraceStore 的指标聚合。"""

import time
from typing import Optional

from app.config.settings import settings


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[k]


def compute_metrics(store, window_hours: Optional[float] = None) -> dict:
    """聚合指标。`window_hours` 为 None 时统计全部历史(与改造前一致)。

    为什么需要时间窗:改造前只有"全部历史"一种口径,于是一个**已经修好**的问题
    会永远留在看板上——修完之后新调用全成功,而累计值被几百条旧失败压着,
    红色要好几周才褪。运维看到的是"改了没用",实际是"口径不对"。

    默认仍是全历史,不是偷懒:调用方(HTTP 端点)显式传窗口,而
    `compute_metrics(store)` 这个既有签名的行为**逐字节不变**——它有既有回归
    钉着,不该因为新增一个能力就改掉旧语义。
    """
    since = None
    if window_hours is not None and window_hours > 0:
        since = time.time() - float(window_hours) * 3600.0
    traces = store.all_traces(since=since)
    spans = store.all_spans(since=since)

    total = len(traces)
    errors = sum(1 for t in traces if t["status"] == "error")
    latencies = [t["latency_ms"] for t in traces]
    prompt_tokens = sum(t["prompt_tokens"] or 0 for t in traces)
    completion_tokens = sum(t["completion_tokens"] or 0 for t in traces)

    tool_spans = [s for s in spans if s["kind"] == "tool"]
    tool_ok = sum(1 for s in tool_spans if s["success"] == 1)

    guard_spans = [s for s in spans if s["kind"] == "guard"]
    guard_blocks = sum(
        1 for s in guard_spans if s.get("meta") and '"action": "block"' in s["meta"]
    )
    guard_sanitizes = sum(
        1 for s in guard_spans if s.get("meta") and '"action": "sanitize"' in s["meta"]
    )

    hitl_spans = [s for s in spans if s["kind"] == "hitl"]

    # KB 召回:**这是本项目最后一条没有出口的降级路径**。
    #
    # 实测过它的后果:ApeRAG 容器停了 24 分钟,`aperag_search` 抛 ConnectError →
    # fail-soft 返回 None → 召回 0 条,而客服照常回答、只是答案里没有任何政策依据
    # (退货运费之类答的是模型常识)。**买家侧零症状,运维侧零信号。**
    #
    # 三个数分开报,因为处置完全不同:
    #   degraded → 知识库连不上/回落本地 → 去修依赖
    #   miss     → 库是通的但这一问没有相关政策 → 可能要补文档
    #   skipped  → 查询理解判定这轮不需要 KB → 正常,不是问题
    # 合成一个"召回率"会把这三件事糊在一起,而只有第一个是故障。
    recall_spans = [s for s in spans if s["kind"] == "recall"]
    kb_spans = [s for s in recall_spans if "kb" in (s.get("name") or "")]
    kb_degraded = sum(1 for s in kb_spans
                      if s.get("meta") and '"degraded": true' in s["meta"].lower())
    kb_skipped = sum(1 for s in kb_spans
                     if s.get("meta") and '"skipped": true' in s["meta"].lower())
    kb_attempted = len(kb_spans) - kb_skipped

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
        "guard_blocks": guard_blocks,
        "guard_sanitizes": guard_sanitizes,
        "block_rate": (guard_blocks / total) if total else 0.0,
        "handoffs": len(hitl_spans),
        "escalation_rate": (len(hitl_spans) / total) if total else 0.0,
        # KB 召回降级:分母是**真正尝试过检索的轮次**(排除门控跳过的),
        # 否则闲聊轮会把这个比率无声稀释——而稀释后的数字正好会在故障时看起来没事。
        "kb_recall_attempts": kb_attempted,
        "kb_recall_degraded": kb_degraded,
        "kb_degraded_rate": (kb_degraded / kb_attempted) if kb_attempted else 0.0,
        "kb_recall_skipped": kb_skipped,
        "intent_distribution": intent_dist,
        # 口径必须跟着数字一起下发。一个百分比脱离了统计窗口就没有意义,而这
        # 几个数字正是运维判断"要不要去看一眼"的依据——看板不能让人自己猜
        # 它统计的是最近一小时还是开服至今。None = 全部历史。
        "window_hours": window_hours,
    }
