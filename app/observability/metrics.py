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
    # 首字时间(TTFT)。**采集早就有,只是从没进过看板。**
    #
    # `reply_delta:first` 这条零时长 span 的 `started_at - trace.started_at` 就是它
    # (见 tracer.py 那段注释)。而看板此前只有 P50/P95 **总延迟** —— 那是"整段
    # 回复生成完"的时间,和买家体感的"多久看到第一个字"是两回事:流式下总时长 12s
    # 但首字 1.5s 是可接受的,首字 12s 则是灾难。**在线客服的核心 KPI 是前者。**
    #
    # 按 trace 关联:一条 trace 至多一个 first span(tracer 只记第一块)。
    #
    # **两个口径都算,报出去的主指标是买家侧那个。** 引擎吐出第一个 token 的时刻
    # (reply_delta)和买家真正看到第一个字的时刻(reply_visible)之间隔着增量脱敏
    # 的 holdback 缓冲。这个差曾经大到让引擎侧的数字失去意义:扣留量按最宽模式
    # 取 114 字符时,75 字的回复一条 delta 都发不出去,引擎侧首字 1 秒多而买家是
    # 等到最后才一次性看到全文。拿引擎侧的数当体感指标,等于报一个买家从来没体
    # 验过的时间。屏障收窄后差值只剩几个字符,但口径不能因为"现在差不多了"就含糊。
    _trace_start = {t["trace_id"]: t["started_at"] for t in traces}

    def _ttft(kind: str) -> list[float]:
        xs = [(s["started_at"] - _trace_start[s["trace_id"]]) * 1000.0
              for s in spans if s["kind"] == kind and s["trace_id"] in _trace_start]
        return [x for x in xs if x >= 0]      # 时钟异常的负值丢掉,不参与分位

    engine_ttfts = _ttft("reply_delta")
    ttfts = _ttft("reply_visible")

    # holdback 代价必须**按 trace 配对**再取分位,不能拿两个 P50 相减:两组样本的
    # 总体不同(reply_visible 上线前的历史 trace 只有引擎侧那条 span),相减出来的
    # 数字没有任何含义——实测就出现过"买家侧只有 5 条样本、引擎侧 30 条"的窗口。
    _engine_at = {s["trace_id"]: s["started_at"] for s in spans if s["kind"] == "reply_delta"}
    _visible_at = {s["trace_id"]: s["started_at"] for s in spans if s["kind"] == "reply_visible"}
    holdback_costs = [
        (_visible_at[tid] - _engine_at[tid]) * 1000.0
        for tid in _visible_at if tid in _engine_at
    ]
    holdback_costs = [x for x in holdback_costs if x >= 0]
    # 兼容 reply_visible 上线之前落库的历史 trace:那时只有引擎侧 span。回退到
    # 引擎侧而不是报 0,否则切到长窗口时看板会凭空多出一段"首字 0 毫秒"的假历史。
    if not ttfts:
        ttfts = engine_ttfts

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
        # 首字时间:买家体感的核心指标。streamed_traces 是分母——没有流式的轮次
        # (快路径直答、转人工短路)不该稀释它,而报出这个分母也让人看得出
        # "这个 P50 是基于多少条算的"。
        "ttft_p50_ms": _percentile(ttfts, 50),
        "ttft_p95_ms": _percentile(ttfts, 95),
        "streamed_traces": len(ttfts),
        # 引擎侧首字,单独报一份作对照:它与上面那个的差值 = 增量脱敏 holdback
        # 的体感代价。差值突然变大意味着有人加了一条更宽的护栏正则,而那件事
        # 在别处没有任何信号。
        "ttft_engine_p50_ms": _percentile(engine_ttfts, 50),
        "ttft_holdback_cost_p50_ms": _percentile(holdback_costs, 50),
        # 配对样本数一并报出:这个代价是基于多少条 trace 算的,看的人有权知道。
        "ttft_holdback_paired": len(holdback_costs),
        "intent_distribution": intent_dist,
        # 口径必须跟着数字一起下发。一个百分比脱离了统计窗口就没有意义,而这
        # 几个数字正是运维判断"要不要去看一眼"的依据——看板不能让人自己猜
        # 它统计的是最近一小时还是开服至今。None = 全部历史。
        "window_hours": window_hours,
    }
