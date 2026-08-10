"""首字时间必须是**买家侧**的口径,不是引擎侧的。

引擎吐出第一个 token(`reply_delta:first`)和买家真正看到第一个字
(`reply_visible:first`)之间隔着增量脱敏的 holdback 缓冲。这个差曾经大到让引擎侧
的数字失去意义:扣留量按最宽模式取 114 字符时,75 字的回复一条 delta 都发不出去
——引擎侧首字一秒多,买家却是等到最后才一次性看到全文。把引擎侧的数摆在看板上
当体感指标,报的是一个买家从来没体验过的时间。

钉四件事:
  1. 两条 span 分别记,不能合并——差值本身是可观测信号(护栏变宽会让它变大);
  2. `ttft_p50_ms` 取买家侧;
  3. 历史 trace(只有引擎侧 span)不会因此变成"首字 0 毫秒"的假数据;
  4. 没有流式的轮次不进分母。
"""

import itertools

from app.observability.metrics import compute_metrics
from app.observability.store import TraceStore
from app.observability.trace import Span, Trace
from app.observability.tracer import Tracer


def _tracer(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    clock = itertools.count()          # 每次读表 +1 秒,方便断言"哪个在前"
    ids = itertools.count(1)
    return Tracer(store, now=lambda: next(clock), id_factory=lambda: f"id{next(ids)}"), store


def _trace_with(tid: str, *, engine_ms=None, visible_ms=None) -> Trace:
    """造一条 trace:引擎首字/买家首字分别落在起点后的指定毫秒处。"""
    tr = Trace(trace_id=tid, session_id="s", user_input="查订单", intent="order",
               started_at=100.0, ended_at=110.0, latency_ms=10000.0,
               status="ok", error=None)
    if engine_ms is not None:
        at = 100.0 + engine_ms / 1000.0
        tr.spans.append(Span(span_id=f"{tid}-e", trace_id=tid, name="reply_delta:first",
                             kind="reply_delta", started_at=at, ended_at=at,
                             latency_ms=0.0, meta={}))
    if visible_ms is not None:
        at = 100.0 + visible_ms / 1000.0
        tr.spans.append(Span(span_id=f"{tid}-v", trace_id=tid, name="reply_visible:first",
                             kind="reply_visible", started_at=at, ended_at=at,
                             latency_ms=0.0, meta={}))
    return tr


def _store(tmp_path, traces) -> TraceStore:
    s = TraceStore(str(tmp_path / "t.db"))
    s.init_schema()
    for tr in traces:
        s.save_trace(tr)
    return s


def test_tracer_records_both_first_spans(tmp_path):
    """Tracer 收到 reply_visible 要单独落一条 span——不是把 reply_delta 那条改个名。"""
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "查订单"):
        tracer.on_event({"type": "reply_delta", "first": True, "content": "订"})
        tracer.on_event({"type": "reply_visible", "first": True})

    kinds = [s["kind"] for s in store.all_spans()]
    assert "reply_delta" in kinds, "引擎侧首字 span 丢了"
    assert "reply_visible" in kinds, "买家侧首字 span 没记——看板只能拿引擎侧凑数"


def test_only_first_visible_chunk_is_recorded(tmp_path):
    """和 reply_delta 同规矩:只记第一块。每块都记会把一条 trace 灌满零信息量的 span。"""
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "查订单"):
        tracer.on_event({"type": "reply_visible", "first": True})
        tracer.on_event({"type": "reply_visible"})      # 后续块不带 first
        tracer.on_event({"type": "reply_visible"})

    visible = [s for s in store.all_spans() if s["kind"] == "reply_visible"]
    assert len(visible) == 1


def test_headline_ttft_uses_buyer_visible_not_engine(tmp_path):
    """引擎侧 2000ms、买家侧 3500ms 时,看板的首字必须报 3500ms。"""
    st = _store(tmp_path, [_trace_with("t0", engine_ms=2000, visible_ms=3500)])
    m = compute_metrics(st)
    assert abs(m["ttft_p50_ms"] - 3500.0) < 1.0, "报的是引擎侧,买家没经历过这个时间"
    assert abs(m["ttft_engine_p50_ms"] - 2000.0) < 1.0
    assert abs(m["ttft_holdback_cost_p50_ms"] - 1500.0) < 1.0, \
        "holdback 代价没算出来——护栏变宽这件事就没有信号了"
    assert m["ttft_holdback_paired"] == 1


def test_holdback_cost_is_paired_per_trace_not_percentile_subtraction(tmp_path):
    """holdback 代价必须按 trace 配对再取分位,不能拿两个 P50 相减。

    造一个真实会出现的窗口:大量历史 trace 只有引擎侧 span(reply_visible 上线
    前落的库),少量新 trace 两条都有。两个总体差一个数量级,分位数相减出来的
    数没有任何含义——实测在 24 小时窗口上撞到过"买家侧 5 条样本 / 引擎侧 30
    条"的情形,那个减法的结果是 0ms,而真实代价是 182ms。
    """
    traces = [_trace_with(f"old{i}", engine_ms=500) for i in range(9)]
    traces.append(_trace_with("new0", engine_ms=4000, visible_ms=4200))
    m = compute_metrics(_store(tmp_path, traces))

    assert m["ttft_holdback_paired"] == 1, "配对样本数不对"
    assert abs(m["ttft_holdback_cost_p50_ms"] - 200.0) < 1.0, \
        "拿两个总体的分位数相减了:引擎侧 P50 被 9 条历史 trace 压到 500ms 附近"


def test_legacy_traces_without_visible_span_fall_back_not_zero(tmp_path):
    """`reply_visible` 上线前落库的 trace 只有引擎侧 span。这类历史数据必须回退
    到引擎侧,而不是被当成"首字 0 毫秒"——否则切到长窗口时看板会凭空多出一段
    完美的假历史,而那正是运维判断"最近是不是变慢了"的依据。"""
    st = _store(tmp_path, [_trace_with("t0", engine_ms=2500)])
    m = compute_metrics(st)
    assert m["ttft_p50_ms"] > 0, "历史 trace 被算成 0 毫秒,凭空造出一段假的好数据"
    assert abs(m["ttft_p50_ms"] - 2500.0) < 1.0
    assert m["streamed_traces"] == 1


def test_non_streamed_turns_do_not_dilute(tmp_path):
    """没有流式的轮次(快路径直答、转人工短路)不进分母——否则它们会把 P50
    稀释成一个谁也没经历过的数;分母为 0 也让人看得出"这个数没有意义"。"""
    st = _store(tmp_path, [_trace_with("t0")])   # 两条 span 都没有
    m = compute_metrics(st)
    assert m["streamed_traces"] == 0
    assert m["ttft_p50_ms"] == 0.0
