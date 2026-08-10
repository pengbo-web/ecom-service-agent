"""工具失败原因的可见性。

**这个盲区是实跑走查抓到的。** 看板显示工具成功率 69.9%——三成调用在失败——
点开任意一条 trace,`meta` 里只有 `{"args": {...}}`。于是"三成工具在失败"这个
结论只能停在这里,查不下去:不知道是哪一类失败、不知道是不是同一个原因。

`guard` 与 `recall` span 早就在记 `reason` 了,工具这一类是漏的。
"""

from __future__ import annotations

import json

from app.observability.tracer import _parse_error, _parse_success


def _trace():
    """最小可保存的 Trace(字段全为必填,这里只给出与断言无关的占位值)。"""
    from app.observability.trace import Trace

    return Trace(trace_id="t1", session_id="s1", user_input="hi", intent=None,
                 started_at=0.0, ended_at=0.1, latency_ms=100.0,
                 status="ok", error=None)


def test_extracts_reason_from_common_shapes():
    """各工具/上游(hmdp)用词不统一,常见字段都要认。"""
    for key in ("errorMsg", "error", "message", "reason", "detail"):
        content = json.dumps({"success": False, key: "订单不存在"}, ensure_ascii=False)
        assert _parse_error(content) == "订单不存在", key


def test_prefers_the_most_specific_field():
    """同时有多个字段时按优先级取,不要拼接。"""
    content = json.dumps({"success": False, "errorMsg": "无权访问", "message": "ok"},
                         ensure_ascii=False)
    assert _parse_error(content) == "无权访问"


def test_does_not_keep_the_whole_response_body():
    """只取原因字段,不留整段响应体。

    工具返回里常带订单、地址、手机号;整段落进 trace 库等于给自己造一份长期
    留存的 PII 副本,而排障需要的只是那一句"订单不存在"。
    """
    content = json.dumps({
        "success": False, "error": "订单不存在",
        "order": {"address": "上海市浦东新区xx路1号", "phone": "13800138000"},
    }, ensure_ascii=False)
    out = _parse_error(content)
    assert out == "订单不存在"
    assert "13800138000" not in out
    assert "浦东" not in out


def test_truncates_long_reasons():
    content = json.dumps({"success": False, "error": "x" * 5000})
    assert len(_parse_error(content)) == 200


def test_non_json_returns_none_rather_than_raw_text():
    """非 JSON 返回可能是任意长度的自由文本(甚至是模型输出),
    留进 meta 会把这个字段变成一个不可控的黑洞。"""
    assert _parse_error("订单不存在,请核对单号后重试" * 100) is None
    assert _parse_error(None) is None
    assert _parse_error("[1,2,3]") is None


def test_missing_reason_field_is_none_not_empty_string():
    """没有原因字段时返回 None,让上层能区分"没有原因"和"原因是空串"。"""
    assert _parse_error(json.dumps({"success": False})) is None
    assert _parse_error(json.dumps({"success": False, "error": "   "})) is None


def test_success_parsing_unchanged():
    assert _parse_success(json.dumps({"success": True})) is True
    assert _parse_success(json.dumps({"success": False})) is False
    assert _parse_success("not json") is None


def test_error_recorded_only_on_failure():
    """成功的返回不记 error。

    成功时没有"原因"可言,而那些字段(message 之类)恰恰最容易带上业务数据。
    """
    import inspect

    from app.observability import tracer

    src = inspect.getsource(tracer.Tracer._dispatch)
    assert "if sp.success is False:" in src


def test_get_trace_decodes_meta_for_api_consumers(tmp_path):
    """API 出口要给出对象而不是"JSON 里套 JSON 字符串"。

    透传字符串会迫使每个消费方各自再 parse 一次,而第一个忘记 parse 的地方
    会静默拿不到字段(`span.meta?.error` 在字符串上恒为 undefined,不报错)。
    """
    from app.observability.store import TraceStore
    from app.observability.trace import Span

    store = TraceStore(str(tmp_path / "t.db"))
    store.init_schema()
    tr = _trace()
    tr.spans.append(Span(span_id="sp1", trace_id="t1", name="tool:query_order",
                         kind="tool", started_at=0.0, ended_at=0.1, latency_ms=100.0,
                         success=False, meta={"args": {}, "error": "订单不存在"}))
    store.save_trace(tr)

    got = store.get_trace("t1")
    assert isinstance(got["spans"][0]["meta"], dict)
    assert got["spans"][0]["meta"]["error"] == "订单不存在"


def test_all_spans_still_returns_raw_meta_string(tmp_path):
    """`all_spans()` 必须保持原样。

    `compute_metrics` 是按**字符串包含**判断护栏动作的
    (`'"action": "block"' in s["meta"]`)。顺手在那边"统一"成对象会当场改坏
    指标口径——两处口径不同是既有事实,这里把它钉住,而不是悄悄改掉。
    """
    from app.observability.store import TraceStore
    from app.observability.trace import Span

    store = TraceStore(str(tmp_path / "t.db"))
    store.init_schema()
    tr = _trace()
    tr.spans.append(Span(span_id="sp1", trace_id="t1", name="guard:x", kind="guard",
                         started_at=0.0, ended_at=0.0, latency_ms=0.0,
                         meta={"action": "block"}))
    store.save_trace(tr)

    assert isinstance(store.all_spans()[0]["meta"], str)


def test_metrics_still_counts_guard_blocks(tmp_path):
    """上一条的行为后果:护栏拦截计数不能因为这次改动变成 0。"""
    from app.observability.metrics import compute_metrics
    from app.observability.store import TraceStore
    from app.observability.trace import Span

    store = TraceStore(str(tmp_path / "t.db"))
    store.init_schema()
    tr = _trace()
    tr.spans.append(Span(span_id="sp1", trace_id="t1", name="guard:x", kind="guard",
                         started_at=0.0, ended_at=0.0, latency_ms=0.0,
                         meta={"action": "block"}))
    store.save_trace(tr)

    assert compute_metrics(store)["guard_blocks"] == 1
