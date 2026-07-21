"""把阻塞式 agent.chat() 桥接成 SSE 事件生成器（W2 tracer + W3 guardrails/consent 可选）。"""

import queue
import threading
from typing import Iterator

from app.guardrails.base import SAFE_FALLBACK

_SENTINEL = object()


def run_agent_streaming(agent, user_input: str, tracer=None,
                        session_id: str = "", guard_pipeline=None,
                        hitl=None, confirm: bool = False) -> Iterator[dict]:
    q: "queue.Queue" = queue.Queue()

    # 本轮授权的风险动作:显式 confirm 标志,或用户这轮说了确认语(退款/成交等才放行)
    from app.agent.consent import RISK_ACTIONS, consent_scope, is_confirmation
    granted = RISK_ACTIONS if (confirm or is_confirmation(user_input)) else frozenset()

    def sink(ev: dict) -> None:
        if tracer is not None:
            tracer.on_event(ev)
        q.put(ev)

    def _blocked_flow(_sink, gr) -> str:
        _sink({"type": "guard", "stage": "input", "action": "block",
               "guard": gr.guard, "reason": gr.reason})
        _sink({"type": "reply", "content": SAFE_FALLBACK})
        _sink({"type": "metadata", "intent": "blocked", "confidence": 1.0,
               "requires_human": False, "follow_up_question": None})
        return "blocked"

    def _normal_flow(_sink) -> str:
        with consent_scope(granted):
            result = agent.chat(user_input)
        reply = result.reply
        if guard_pipeline is not None:
            reply, out_results = guard_pipeline.check_output(reply)
            for gr in out_results:
                _sink({"type": "guard", "stage": "output", "action": gr.action,
                       "guard": gr.guard, "reason": gr.reason})
        _sink({"type": "reply", "content": reply})
        _sink({"type": "metadata", "intent": result.intent.value,
               "confidence": result.confidence,
               "requires_human": result.requires_human,
               "follow_up_question": result.follow_up_question})
        if hitl is not None:
            reasons = hitl.evaluate(result.intent.value, result.confidence,
                                    result.requires_human, user_input=user_input)
            if reasons:
                recent = list(getattr(agent, "raw_messages", []))[-6:]
                hid = hitl.escalate(session_id, user_input, reply,
                                    result.intent.value, result.confidence,
                                    reasons, recent_context=recent)
                _sink({"type": "handoff", "reasons": reasons, "handoff_id": hid})
        return result.intent.value

    def _drive(_sink) -> str:
        if guard_pipeline is not None:
            gin = guard_pipeline.check_input(user_input)
            if gin.action == "block":
                return _blocked_flow(_sink, gin)
        return _normal_flow(_sink)

    def worker():
        real_client = getattr(agent, "client", None)
        agent.event_sink = sink
        try:
            if tracer is None:
                _drive(sink)
            else:
                from app.observability.client_proxy import TracingClient
                with tracer.start_trace(session_id, user_input) as trace:
                    if real_client is not None:
                        agent.client = TracingClient(real_client, tracer)
                    try:
                        trace.intent = _drive(sink)
                    finally:
                        if real_client is not None:
                            agent.client = real_client
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "message": str(e)})
        finally:
            agent.event_sink = None
            q.put(_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        event = q.get()
        if event is _SENTINEL:
            yield {"type": "done"}
            return
        yield event
