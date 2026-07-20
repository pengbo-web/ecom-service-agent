"""把阻塞式 agent.chat() 桥接成可被 SSE 消费的事件生成器（W2：可选接入 tracer）。"""

import queue
import threading
from typing import Iterator

_SENTINEL = object()


def run_agent_streaming(agent, user_input: str, tracer=None,
                        session_id: str = "") -> Iterator[dict]:
    """在后台线程运行 agent.chat()，把过程事件 + 最终结果按序 yield 出来。

    tracer 非空时：整次请求包一条 Trace，工具事件配对成 span，
    LLM 调用经 TracingClient 采集 token/延迟。tracer 为空时行为与 W1 一致。
    """
    q: "queue.Queue" = queue.Queue()

    def _run(sink):
        result = agent.chat(user_input)
        sink({"type": "reply", "content": result.reply})
        sink({
            "type": "metadata",
            "intent": result.intent.value,
            "confidence": result.confidence,
            "requires_human": result.requires_human,
            "follow_up_question": result.follow_up_question,
        })
        return result

    def worker():
        if tracer is None:
            agent.event_sink = q.put
            try:
                _run(q.put)
            except Exception as e:  # noqa: BLE001
                q.put({"type": "error", "message": str(e)})
            finally:
                agent.event_sink = None
                q.put(_SENTINEL)
            return

        # 接入 tracer 分支
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
                    result = _run(sink)
                    trace.intent = result.intent.value
                finally:
                    agent.event_sink = None
                    if real_client is not None:
                        agent.client = real_client
        except Exception as e:  # noqa: BLE001 —— start_trace 已记录 error 状态
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
