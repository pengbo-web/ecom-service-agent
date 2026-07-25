"""Tracer：用 contextvar 维护当前 Trace，提供 span 与事件拦截。"""

import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from app.observability.trace import Trace, Span

_current: ContextVar[Optional[Trace]] = ContextVar("current_trace", default=None)


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
            _current.reset(token)
            try:
                self.store.save_trace(trace)
            except Exception:  # 落库失败不应影响主流程
                pass

    @contextmanager
    def span(self, name: str, kind: str):
        trace = _current.get()
        start = self._now()
        sp = Span(span_id=self._id(), trace_id=trace.trace_id if trace else "",
                  name=name, kind=kind, started_at=start, ended_at=start,
                  latency_ms=0.0)
        try:
            yield sp
        finally:
            sp.ended_at = self._now()
            sp.latency_ms = (sp.ended_at - sp.started_at) * 1000.0
            if trace is not None:
                trace.spans.append(sp)

    def on_event(self, event: dict) -> None:
        trace = _current.get()
        if trace is None:
            return
        etype = event.get("type")
        if etype == "tool_call":
            start = self._now()
            self._pending_tool.append(Span(
                span_id=self._id(), trace_id=trace.trace_id,
                name=f"tool:{event.get('name')}", kind="tool",
                started_at=start, ended_at=start, latency_ms=0.0,
                meta={"args": event.get("args", {})},
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
            ))
        elif etype == "recall":
            now = self._now()
            trace.spans.append(Span(
                span_id=self._id(), trace_id=trace.trace_id,
                name=f"recall:{event.get('source')}", kind="recall",
                started_at=now, ended_at=now, latency_ms=0.0,
                meta={"query": event.get("query"), "hits": event.get("hits", []),
                      "skipped": event.get("skipped"), "reason": event.get("reason")},
            ))
        elif etype == "handoff":
            now = self._now()
            trace.spans.append(Span(
                span_id=self._id(), trace_id=trace.trace_id,
                name="handoff", kind="hitl",
                started_at=now, ended_at=now, latency_ms=0.0,
                meta={"reasons": event.get("reasons", [])},
            ))


def _parse_success(content) -> Optional[bool]:
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "success" in data:
            return bool(data["success"])
    except Exception:
        pass
    return None
