import json
import itertools

from app.observability.store import TraceStore
from app.observability.tracer import Tracer


def _tracer(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    clock = itertools.count(start=0, step=1)          # 确定性时钟：0,1,2,...
    ids = itertools.count(start=1)
    return Tracer(store, now=lambda: next(clock),
                  id_factory=lambda: f"id{next(ids)}"), store


def _meta(span: dict) -> dict:
    """store 落库时把 meta 序列化成了 JSON 字符串(见 TraceStore.save_trace)，
    读出来也是原样字符串，测试里要断言字段得先解回 dict。"""
    return json.loads(span["meta"])


def test_start_trace_persists_on_exit(tmp_path):
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "你好") as t:
        t.intent = "greeting"
    saved = store.get_trace(t.trace_id)
    assert saved is not None
    assert saved["status"] == "ok"
    assert saved["intent"] == "greeting"
    assert saved["latency_ms"] >= 0


def test_span_attached_to_trace(tmp_path):
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "hi") as t:
        with tracer.span("llm.chat.create", "llm") as sp:
            sp.prompt_tokens = 50
            sp.completion_tokens = 10
    saved = store.get_trace(t.trace_id)
    assert len(saved["spans"]) == 1
    assert saved["spans"][0]["kind"] == "llm"
    assert saved["prompt_tokens"] == 50


def test_on_event_pairs_tool_spans(tmp_path):
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "查订单") as t:
        tracer.on_event({"type": "tool_call", "name": "query_order", "args": {"id": "A"}})
        tracer.on_event({"type": "tool_result",
                         "content": json.dumps({"success": True})})
    saved = store.get_trace(t.trace_id)
    tool_spans = [s for s in saved["spans"] if s["kind"] == "tool"]
    assert len(tool_spans) == 1
    assert tool_spans[0]["name"] == "tool:query_order"
    assert tool_spans[0]["success"] == 1


def test_error_status_recorded(tmp_path):
    tracer, store = _tracer(tmp_path)
    try:
        with tracer.start_trace("sess1", "boom") as t:
            raise RuntimeError("炸了")
    except RuntimeError:
        pass
    saved = store.get_trace(t.trace_id)
    assert saved["status"] == "error"
    assert "炸了" in saved["error"]


# ── W1:stage 嵌套 + 新事件类型覆盖 ──────────────────────────────────────────

def test_stage_events_nest_with_real_durations(tmp_path):
    """react 包工具调用；reply_pipeline 包 evaluate/polish 子阶段——嵌套顺序
    与耗时都要如实反映(确定性时钟:每次 _now() 递增 1,latency 应为差值)。"""
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "查订单") as t:
        tracer.on_event({"type": "stage", "status": "start", "name": "react"})
        tracer.on_event({"type": "tool_call", "name": "query_order", "args": {}})
        tracer.on_event({"type": "tool_result", "content": '{"success": true}'})
        tracer.on_event({"type": "stage", "status": "end", "name": "react"})

        tracer.on_event({"type": "stage", "status": "start", "name": "reply_pipeline"})
        tracer.on_event({"type": "select", "next": "evaluate", "reason": "rule_selector"})
        tracer.on_event({"type": "stage", "status": "start", "name": "evaluate"})
        tracer.on_event({"type": "evaluate", "ok": True})
        tracer.on_event({"type": "stage", "status": "end", "name": "evaluate"})
        tracer.on_event({"type": "stage", "status": "end", "name": "reply_pipeline"})

    saved = store.get_trace(t.trace_id)
    by_name = {s["name"]: s for s in saved["spans"]}

    react = by_name["stage:react"]
    reply_pipeline = by_name["stage:reply_pipeline"]
    evaluate_stage = by_name["stage:evaluate"]
    tool_span = by_name["tool:query_order"]
    select_span = by_name["select:evaluate"]
    evaluate_marker = by_name["evaluate"]

    # 顶层阶段互不为父，都直接挂在 trace 下
    assert react["parent_span_id"] is None
    assert reply_pipeline["parent_span_id"] is None
    # 工具调用嵌在 react 内；select/evaluate 子阶段嵌在 reply_pipeline 内；
    # evaluate 判定标记又嵌在 stage:evaluate 内 —— 三层嵌套关系如实还原
    assert tool_span["parent_span_id"] == react["span_id"]
    assert select_span["parent_span_id"] == reply_pipeline["span_id"]
    assert evaluate_stage["parent_span_id"] == reply_pipeline["span_id"]
    assert evaluate_marker["parent_span_id"] == evaluate_stage["span_id"]
    # 真实时长:确定性时钟下 latency_ms 应为正且等于 (ended-started)*1000
    for sp in (react, reply_pipeline, evaluate_stage):
        assert sp["latency_ms"] > 0
        assert sp["latency_ms"] == (sp["ended_at"] - sp["started_at"]) * 1000.0


def test_unbalanced_stage_end_without_start_is_ignored(tmp_path):
    """落单的 end(没有配对的 start):必须被安静忽略,不能生造出一个假 span，
    也不能把后续正常事件的嵌套关系带歪。"""
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "hi") as t:
        tracer.on_event({"type": "stage", "status": "end", "name": "react"})  # 落单 end
        tracer.on_event({"type": "thought", "content": "嗯"})
    saved = store.get_trace(t.trace_id)
    assert [s["kind"] for s in saved["spans"]] == ["thought"]
    assert saved["spans"][0]["parent_span_id"] is None   # 没有被落单 end 污染出假父节点


def test_stage_left_open_by_error_is_force_closed_and_marked(tmp_path):
    """一轮跑到某个 stage 里途中抛异常、且没有走到匹配的 end:trace 结束时必须
    强制收口这个悬空 span(不能永久丢失),并打上 unbalanced 标记方便排查；
    trace 本身仍完整落库、status 正确记为 error，不会被这半条 stage 拖崩。"""
    tracer, store = _tracer(tmp_path)
    try:
        with tracer.start_trace("sess1", "boom") as t:
            tracer.on_event({"type": "stage", "status": "start", "name": "react"})
            tracer.on_event({"type": "tool_call", "name": "query_order", "args": {}})
            raise RuntimeError("react 内部炸了")
            # 注意:没有发出 stage end / tool_result —— 模拟异常打断
    except RuntimeError:
        pass

    saved = store.get_trace(t.trace_id)
    assert saved["status"] == "error"
    by_name = {s["name"]: s for s in saved["spans"]}
    react = by_name["stage:react"]
    assert _meta(react)["unbalanced"] is True
    assert react["ended_at"] == saved["ended_at"]   # 按 trace 结束时刻强制收口
    assert react["latency_ms"] >= 0
    # 未关闭的 tool_call 同样没有被悬空丢弃——tracer 的 _pending_tool 栈本身
    # 不受这条异常影响(它只在 tool_result 到来时才出栈)，此处只断言 trace
    # 没有因为这半条 stage 崩掉、已发生的其它事件仍完整可查。
    assert "stage:react" in by_name


def test_marker_events_each_produce_expected_span(tmp_path):
    """新覆盖的 9 类事件里，除 stage 外全部是零时长标记：一次性记完，
    kind 即事件类型本身，workflow_guard/degrade 独立 kind 便于前端标出。"""
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "咨询") as t:
        tracer.on_event({"type": "route", "agent": "售前客服", "key": "presale"})
        tracer.on_event({"type": "workflow_guard", "name": "refund",
                         "reason": "需先查单"})
        tracer.on_event({"type": "degrade", "reason": "empty_reply"})
        tracer.on_event({"type": "faq_cache", "matched": "如何退货", "score": 0.92})
        tracer.on_event({"type": "skill_preloaded", "name": "process-return",
                         "variant": "live"})
        tracer.on_event({"type": "thought", "content": "我需要先查订单"})
        tracer.on_event({"type": "evaluate", "ok": False})
        tracer.on_event({"type": "polish"})
        tracer.on_event({"type": "select", "next": "done", "reason": "already_polished"})

    saved = store.get_trace(t.trace_id)
    by_kind = {s["kind"]: s for s in saved["spans"]}

    assert by_kind["route"]["name"] == "route:售前客服"
    assert _meta(by_kind["route"])["key"] == "presale"

    assert by_kind["workflow_guard"]["name"] == "workflow_guard:refund"
    assert _meta(by_kind["workflow_guard"])["reason"] == "需先查单"

    assert by_kind["degrade"]["name"] == "degrade:empty_reply"

    assert _meta(by_kind["faq_cache"])["matched"] == "如何退货"

    assert by_kind["skill_preloaded"]["name"] == "skill_preloaded:process-return"
    assert _meta(by_kind["skill_preloaded"])["variant"] == "live"

    assert _meta(by_kind["thought"])["content"] == "我需要先查订单"

    assert _meta(by_kind["evaluate"])["ok"] is False

    assert by_kind["polish"]["name"] == "polish"

    assert by_kind["select"]["name"] == "select:done"

    # 全部是零时长标记:latency_ms 恒为 0，started_at == ended_at
    for kind in ("route", "workflow_guard", "degrade", "faq_cache",
                 "skill_preloaded", "thought", "evaluate", "polish", "select"):
        sp = by_kind[kind]
        assert sp["latency_ms"] == 0.0
        assert sp["started_at"] == sp["ended_at"]


def test_tracer_failure_cannot_break_a_turn(tmp_path, monkeypatch):
    """观测层内部炸了(比如某个新事件类型的处理逻辑有 bug)：on_event 必须自己
    吞掉，绝不能让异常冒泡到调用方(streaming.py 的 sink 那里没有额外的
    try/except 兜底)、更不能打断这一轮正在进行的真实对话。"""
    tracer, store = _tracer(tmp_path)

    def _boom(trace, event):
        raise RuntimeError("观测内部炸了")

    # 直接赋到实例属性(绕开描述符协议，不会被自动绑定 self)：
    # on_event 内部就是 self._dispatch(trace, event)，两个位置参数正好对上。
    monkeypatch.setattr(tracer, "_dispatch", _boom)

    with tracer.start_trace("sess1", "你好") as t:
        tracer.on_event({"type": "thought", "content": "不应该导致异常冒泡"})
        # 走到这里就说明 on_event 没有把异常甩出来——这一行本身就是断言
        t.intent = "greeting"

    saved = store.get_trace(t.trace_id)
    assert saved["status"] == "ok"   # 观测炸了不该把这一轮标记为 error
    assert saved["intent"] == "greeting"
