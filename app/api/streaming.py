"""把阻塞式 agent.chat() 桥接成 SSE 事件生成器（W2 tracer + W3 guardrails 可选）。"""

import queue
import threading
from typing import Iterator

from app.guardrails.base import SAFE_FALLBACK

_SENTINEL = object()


def run_agent_streaming(agent, user_input: str, tracer=None,
                        session_id: str = "", guard_pipeline=None,
                        hitl=None) -> Iterator[dict]:
    q: "queue.Queue" = queue.Queue()

    def _blocked_flow(sink, gr) -> str:
        """输入被护栏拦截：短路，不调用 Agent。返回 intent 供 trace 记录。"""
        sink({"type": "guard", "stage": "input", "action": "block",
              "guard": gr.guard, "reason": gr.reason})
        sink({"type": "reply", "content": SAFE_FALLBACK})
        sink({"type": "metadata", "intent": "blocked", "confidence": 1.0,
              "requires_human": False, "follow_up_question": None})
        return "blocked"

    def _normal_flow(sink) -> str:
        """正常调用 Agent，并对输出跑护栏。返回 intent。"""
        result = agent.chat(user_input)
        reply = result.reply
        if guard_pipeline is not None:
            reply, out_results = guard_pipeline.check_output(reply)
            for gr in out_results:
                sink({"type": "guard", "stage": "output", "action": gr.action,
                      "guard": gr.guard, "reason": gr.reason})
        sink({"type": "reply", "content": reply})
        sink({"type": "metadata", "intent": result.intent.value,
              "confidence": result.confidence,
              "requires_human": result.requires_human,
              "follow_up_question": result.follow_up_question})
        if hitl is not None:
            reasons = hitl.evaluate(result.intent.value, result.confidence,
                                    result.requires_human)
            if reasons:
                recent = list(getattr(agent, "raw_messages", []))[-6:]
                hid = hitl.escalate(session_id, user_input, reply,
                                    result.intent.value, result.confidence,
                                    reasons, recent_context=recent)
                sink({"type": "handoff", "reasons": reasons, "handoff_id": hid})
        return result.intent.value

    def _drive(sink) -> str:
        """返回 intent 字符串；抛错时上层处理。"""
        if guard_pipeline is not None:
            gin = guard_pipeline.check_input(user_input)
            if gin.action == "block":
                return _blocked_flow(sink, gin)
        return _normal_flow(sink)

    def worker():
        if tracer is None:
            agent.event_sink = q.put
            try:
                _drive(q.put)
            except Exception as e:  # noqa: BLE001
                q.put({"type": "error", "message": str(e)})
            finally:
                agent.event_sink = None
                q.put(_SENTINEL)
            return

        from app.observability.client_proxy import TracingClient

        def sink(ev):
            tracer.on_event(ev)
            q.put(ev)

        real_client = getattr(agent, "client", None)
        try:
            with tracer.start_trace(session_id, user_input) as trace:
                agent.event_sink = sink
                if real_client is not None:
                    agent.client = TracingClient(real_client, tracer)
                try:
                    trace.intent = _drive(sink)
                finally:
                    agent.event_sink = None
                    if real_client is not None:
                        agent.client = real_client
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        event = q.get()
        if event is _SENTINEL:
            yield {"type": "done"}
            return
        yield event
