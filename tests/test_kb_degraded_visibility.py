"""KB 召回降级的可见性 —— 本项目**最后一条**曾经没有出口的降级路径。

实测过它的后果:ApeRAG 容器停了 24 分钟,`aperag_search` 抛 ConnectError →
fail-soft 返回 None → 召回 0 条,而客服**照常回答**、只是答案里没有任何政策依据
(退货运费之类答的是模型常识)。**买家侧零症状,运维侧零信号。**

改造前 `chat.py` 只在两种情况发 recall 事件:命中、门控跳过。"调用失败/召回 0 条"
那一支什么都不发,于是这件事在 trace 与看板上都不存在。
"""

from __future__ import annotations

import json

from app.observability.metrics import compute_metrics
from app.observability.store import TraceStore
from app.observability.trace import Span, Trace


def _trace(tid: str, meta: dict) -> Trace:
    tr = Trace(trace_id=tid, session_id="s", user_input="退货运费谁出", intent=None,
               started_at=100.0, ended_at=100.1, latency_ms=100.0,
               status="ok", error=None)
    tr.spans.append(Span(span_id=f"{tid}-sp", trace_id=tid, name="recall:kb",
                         kind="recall", started_at=100.0, ended_at=100.0,
                         latency_ms=0.0, success=True, meta=meta))
    return tr


def _store(tmp_path, metas: list[dict]) -> TraceStore:
    s = TraceStore(str(tmp_path / "t.db"))
    s.init_schema()
    for i, m in enumerate(metas):
        s.save_trace(_trace(f"t{i}", m))
    return s


def test_degraded_recall_is_counted(tmp_path):
    st = _store(tmp_path, [
        {"degraded": True, "outcome": "unavailable", "backend": "aperag", "hits": []},
        {"degraded": False, "backend": "aperag", "hits": [{"doc": "退换货政策"}]},
    ])
    m = compute_metrics(st)
    assert m["kb_recall_attempts"] == 2
    assert m["kb_recall_degraded"] == 1
    assert m["kb_degraded_rate"] == 0.5


def test_gated_skips_are_excluded_from_the_denominator(tmp_path):
    """分母只算**真正尝试过检索**的轮次。

    闲聊轮(查询理解判定不需要 KB)会把这个比率无声稀释 —— 而稀释后的数字
    恰好会在故障时看起来没事:10 轮闲聊 + 1 轮真故障 = 9%,躲过 10% 的红线。
    """
    st = _store(tmp_path, [
        {"degraded": True, "outcome": "unavailable", "hits": []},
        {"skipped": True, "reason": "greeting", "hits": []},
        {"skipped": True, "reason": "greeting", "hits": []},
    ])
    m = compute_metrics(st)
    assert m["kb_recall_skipped"] == 2
    assert m["kb_recall_attempts"] == 1
    assert m["kb_degraded_rate"] == 1.0, "唯一一次真检索就是降级的,应报 100%"


def test_no_attempts_reports_zero_not_nan(tmp_path):
    """一次都没检索过时不能除零,也不能报成 100%。"""
    m = compute_metrics(_store(tmp_path, [{"skipped": True, "hits": []}]))
    assert m["kb_recall_attempts"] == 0
    assert m["kb_degraded_rate"] == 0.0


def test_fallback_to_local_counts_as_degraded(tmp_path):
    """ApeRAG 挂了但本地索引兜底顶上 —— **这仍然是降级**。

    改造前 `kb.py` 回落时 `return ..., "local", None` 把 meta 丢了,于是
    "aperag 挂了兜底顶上"与"本来就配的是 local"在观测上完全无法区分,
    而前者是需要去修依赖的故障。
    """
    st = _store(tmp_path, [{"degraded": True, "outcome": "unavailable",
                            "backend": "local", "hits": [{"doc": "x"}]}])
    assert compute_metrics(st)["kb_recall_degraded"] == 1


def test_emit_covers_the_miss_branch():
    """`chat.py` 的"没命中且非门控跳过"那一支必须发事件。

    这条钉的是**接线**:指标算得再好,事件不发就永远是 0。
    """
    import inspect

    from app.agent import chat

    src = inspect.getsource(chat)
    i = src.index('"skipped": True, "reason": qu.intent')
    tail = src[i:i + 2000]
    assert '"miss": True' in tail, "miss 分支必须发 recall 事件"
    assert '"degraded"' in tail


def test_tracer_persists_degraded_flag():
    """事件发了但 tracer 不落库,trace 里照样查不到。"""
    import inspect

    from app.observability import tracer

    src = inspect.getsource(tracer)
    i = src.index('elif etype == "recall"')
    tail = src[i:i + 1200]
    for field in ('"degraded"', '"outcome"', '"backend"'):
        assert field in tail, f"recall span 的 meta 必须带 {field}"


def test_hit_branch_also_carries_degraded():
    """**命中分支也必须标 degraded** —— 这是实测抓到的漏网之鱼。

    ApeRAG 挂掉、本地索引兜底顶上时,这一轮**有命中**(走命中分支)但依据来自
    本地旧索引而不是线上知识库。实测停掉 aperag-api 后仍然 hits=2、backend=local
    ——只在 miss 分支标 degraded 会漏掉**最常见的那一种**
    (`kb_local_fallback_enabled` 默认开着)。

    我第一版就漏了这个,而单测当时是绿的:因为那些用例手工构造 meta 去测**指标
    计算**,没有覆盖**发射路径**。这条补的正是发射路径。
    """
    import inspect

    from app.agent import chat

    src = inspect.getsource(chat)
    i = src.index("if rr.kb_hits:")
    hit_branch = src[i:i + 600]
    assert '"degraded": _degraded' in hit_branch, "命中分支必须带 degraded"


def test_degradation_verdict_is_computed_once():
    """降级判据只算一处,两个分支共用。

    命中与未命中各写一遍判据,迟早会漂移成两套口径——而漂移的表现是
    "有时报降级有时不报",最难查的那种。
    """
    import inspect

    from app.agent import chat

    src = inspect.getsource(chat)
    assert src.count("fell_back_to_local") == 1, "判据应只出现一次(共用变量)"
