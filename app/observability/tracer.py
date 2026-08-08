"""Tracer：用 contextvar 维护当前 Trace，提供 span 与事件拦截。

W1 补齐:与 Langfuse 桥(app/observability/langfuse_bridge.py)对齐事件覆盖——
`stage` 用栈组装嵌套 span 树(真实时长)，`route`/`workflow_guard`/`degrade`/
`faq_cache`/`skill_preloaded`/`thought`/`evaluate`/`polish`/`select` 记为
挂在当前 stage 下的零时长标记 span（协议里没有与之配对的起止信号，是"发生了
没有"的瞬时事实，不是要展示的时长区间——判断依据见各分支注释，与 Langfuse
桥的取舍逐一对应)。`workflow_guard`(拦截了什么)与 `degrade`(降级到备选)
各自独立 kind，供前端一眼标出，不与普通步骤混在一起。
"""

import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from app.observability.trace import Trace, Span

_current: ContextVar[Optional[Trace]] = ContextVar("current_trace", default=None)

# 零时长标记事件:协议里没有配对的 start/end 信号，一次性记完。kind 即 etype，
# 前端据此区分普通步骤与需要醒目呈现的安全/失败信号(workflow_guard/degrade)。
_MARKER_TYPES = {
    "route", "workflow_guard", "degrade", "faq_cache",
    "skill_preloaded", "thought", "evaluate", "polish", "select",
}


def _marker_name(etype: str, event: dict) -> str:
    """标记 span 的展示名。可读信息（agent/工具名/理由/next）拼进名字里，
    这样前端哪怕不展开 meta 也能一眼看出这一条讲的是什么。"""
    if etype == "route":
        return f"route:{event.get('agent')}"
    if etype == "workflow_guard":
        return f"workflow_guard:{event.get('name')}"
    if etype == "degrade":
        return f"degrade:{event.get('reason')}"
    if etype == "skill_preloaded":
        return f"skill_preloaded:{event.get('name')}"
    if etype == "select":
        return f"select:{event.get('next')}"
    return etype   # faq_cache / thought / evaluate / polish：名称即类型足够


class Tracer:
    def __init__(self, store, now=time.time, id_factory=None):
        self.store = store
        self._now = now
        self._id = id_factory or (lambda: uuid.uuid4().hex[:16])
        self._pending_tool: list = []  # 已开、未关的工具 span 栈

    def current_trace(self) -> Optional[Trace]:
        return _current.get()

    @contextmanager
    def start_trace(self, session_id: str, user_input: str):
        start = self._now()
        trace = Trace(
            trace_id=self._id(), session_id=session_id, user_input=user_input,
            intent=None, started_at=start, ended_at=start, latency_ms=0.0,
            status="ok", error=None, spans=[],
        )
        token = _current.set(trace)
        try:
            yield trace
        except Exception as e:  # noqa: BLE001
            trace.status = "error"
            trace.error = str(e)
            raise
        finally:
            trace.ended_at = self._now()
            trace.latency_ms = (trace.ended_at - trace.started_at) * 1000.0
            # 兜底收尾:trace 结束时 stage 栈里若还有未关闭的阶段(事件错配、
            # 或本轮在某个 stage 内部异常且异常没有被途中的 try/finally 兜到
            # 发出配对的 end)，按 trace 结束时刻强制收口——保证这些 span 仍被
            # 记录（不"跑丢"），同时打上 unbalanced 标记方便排查，绝不让半条
            # 轨迹污染其它 span 或让 save_trace 崩掉。这一步本身也要 fail-soft：
            # 万一栈里数据被什么意外污染，宁可丢这几条 span，也不能因此跳过
            # 下面必须执行的 _current.reset(token)（否则 contextvar 泄漏，
            # 后续请求会错误地续到这条已经结束的 trace 上）。
            try:
                self._drain_stage_stack(trace)
            except Exception:
                pass
            _current.reset(token)
            try:
                self.store.save_trace(trace)
            except Exception:  # 落库失败不应影响主流程
                pass

    def _drain_stage_stack(self, trace: Trace) -> None:
        stack = getattr(trace, "_stage_stack", None)
        while stack:
            sp = stack.pop()
            sp.ended_at = trace.ended_at
            sp.latency_ms = (sp.ended_at - sp.started_at) * 1000.0
            sp.meta = {**sp.meta, "unbalanced": True}
            trace.spans.append(sp)

    @contextmanager
    def span(self, name: str, kind: str):
        trace = _current.get()
        start = self._now()
        sp = Span(span_id=self._id(), trace_id=trace.trace_id if trace else "",
                  name=name, kind=kind, started_at=start, ended_at=start,
                  latency_ms=0.0, parent_span_id=self._parent(trace) if trace else None)
        try:
            yield sp
        finally:
            sp.ended_at = self._now()
            sp.latency_ms = (sp.ended_at - sp.started_at) * 1000.0
            if trace is not None:
                trace.spans.append(sp)

    # -- stage 嵌套栈 ---------------------------------------------------------
    def _stage_stack(self, trace: Trace) -> list:
        """当前 trace 的 stage 嵌套栈。挂在 trace 实例上（而不是 Tracer 实例
        上）天然按 trace 隔离：每条 trace 只在发起它的那个请求线程内活动，
        不会跟并发的另一条 trace 共享同一个栈，无需额外加锁。"""
        stack = getattr(trace, "_stage_stack", None)
        if stack is None:
            stack = []
            trace._stage_stack = stack
        return stack

    def _parent(self, trace: Optional[Trace]) -> Optional[str]:
        if trace is None:
            return None
        stack = self._stage_stack(trace)
        return stack[-1].span_id if stack else None

    def _on_stage(self, trace: Trace, event: dict) -> None:
        """stage 事件：包裹 ReAct 循环 / 回复流水线及其子角色的嵌套阶段。
        与 Langfuse 桥同构地用栈维护当前嵌套路径——start 入栈（先占位，尚不
        知道时长），end 出栈并结算真实 latency_ms 再落进 trace.spans；栈内
        其它事件都以栈顶为 parent，从而自然呈现"谁包着谁、各花了多久"。
        """
        name = event.get("name") or "stage"
        status = event.get("status")
        stack = self._stage_stack(trace)
        if status == "start":
            now = self._now()
            stack.append(Span(
                span_id=self._id(), trace_id=trace.trace_id,
                name=f"stage:{name}", kind="stage",
                started_at=now, ended_at=now, latency_ms=0.0,
                parent_span_id=(stack[-1].span_id if stack else None),
            ))
        elif status == "end":
            if not stack:
                return   # 落单的 end（没有匹配的 start）：忽略，不生造 span
            sp = stack.pop()
            sp.ended_at = self._now()
            sp.latency_ms = (sp.ended_at - sp.started_at) * 1000.0
            trace.spans.append(sp)
        # 协议里 status 只有 start/end，其它值忽略（fail-soft，不抛异常）

    def _marker(self, trace: Trace, etype: str, event: dict) -> None:
        """零时长标记 span：挂在当前 stage 栈顶下，meta 原样收纳事件的其它字段
        （除 type），不逐字段白名单——协议加字段时这里自动跟上，不用改代码。"""
        now = self._now()
        meta = {k: v for k, v in event.items() if k != "type"}
        trace.spans.append(Span(
            span_id=self._id(), trace_id=trace.trace_id,
            name=_marker_name(etype, event), kind=etype,
            started_at=now, ended_at=now, latency_ms=0.0,
            meta=meta, parent_span_id=self._parent(trace),
        ))

    def on_event(self, event: dict) -> None:
        """观测挂载点：调用方(streaming.py 的 sink)在业务事件流里直接同步调用，
        中间没有 try/except 兜底——观测这一层出任何异常都绝不能反过来打断
        正在进行的真实对话，所以整段派发逻辑在这里自我兜底(与 save_trace
        落库失败即吞的姿态一致)。"""
        trace = _current.get()
        if trace is None:
            return
        try:
            self._dispatch(trace, event)
        except Exception:  # noqa: BLE001 观测失败不能影响主流程
            pass

    def _dispatch(self, trace: Trace, event: dict) -> None:
        etype = event.get("type")
        if etype == "stage":
            self._on_stage(trace, event)
        elif etype == "tool_call":
            start = self._now()
            self._pending_tool.append(Span(
                span_id=self._id(), trace_id=trace.trace_id,
                name=f"tool:{event.get('name')}", kind="tool",
                started_at=start, ended_at=start, latency_ms=0.0,
                meta={"args": event.get("args", {})},
                parent_span_id=self._parent(trace),
            ))
        elif etype == "tool_result" and self._pending_tool:
            sp = self._pending_tool.pop()
            sp.ended_at = self._now()
            sp.latency_ms = (sp.ended_at - sp.started_at) * 1000.0
            sp.success = _parse_success(event.get("content"))
            trace.spans.append(sp)
        elif etype == "guard":
            now = self._now()
            trace.spans.append(Span(
                span_id=self._id(), trace_id=trace.trace_id,
                name=f"guard:{event.get('guard')}", kind="guard",
                started_at=now, ended_at=now, latency_ms=0.0,
                meta={"stage": event.get("stage"), "action": event.get("action"),
                      "reason": event.get("reason")},
                parent_span_id=self._parent(trace),
            ))
        elif etype == "recall":
            now = self._now()
            trace.spans.append(Span(
                span_id=self._id(), trace_id=trace.trace_id,
                name=f"recall:{event.get('source')}", kind="recall",
                started_at=now, ended_at=now, latency_ms=0.0,
                meta={"query": event.get("query"), "hits": event.get("hits", []),
                      "skipped": event.get("skipped"), "reason": event.get("reason")},
                parent_span_id=self._parent(trace),
            ))
        elif etype == "reply_delta" and event.get("first"):
            # E1(回复流式化):只记**第一块**——每块都记会把一条 trace 灌满
            # 上百条零信息量的 span。这一条零时长标记的 `started_at - trace.
            # started_at` 就是"首字时间"(time to first chunk),与总时长
            # (trace.latency_ms)分开看才能验证"是不是从生成一开始就在吐字"，
            # 不是等全量生成完再假装分块发。复用既有 reply_delta 事件本身
            # (`_can_stream_first_step`/`_llm_create_streaming` 已经在发的
            # 那个事件)判断，没有另开一条通道。
            now = self._now()
            trace.spans.append(Span(
                span_id=self._id(), trace_id=trace.trace_id,
                name="reply_delta:first", kind="reply_delta",
                started_at=now, ended_at=now, latency_ms=0.0,
                meta={"time_to_first_chunk_ms": (now - trace.started_at) * 1000.0},
                parent_span_id=self._parent(trace),
            ))
        elif etype == "handoff":
            now = self._now()
            trace.spans.append(Span(
                span_id=self._id(), trace_id=trace.trace_id,
                name="handoff", kind="hitl",
                started_at=now, ended_at=now, latency_ms=0.0,
                meta={"reasons": event.get("reasons", [])},
                parent_span_id=self._parent(trace),
            ))
        elif etype in _MARKER_TYPES:
            self._marker(trace, etype, event)


def _parse_success(content) -> Optional[bool]:
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "success" in data:
            return bool(data["success"])
    except Exception:
        pass
    return None
