"""把阻塞式 agent.chat() 桥接成可被 SSE 消费的事件生成器。"""

import queue
import threading
from typing import Iterator

_SENTINEL = object()


def run_agent_streaming(agent, user_input: str) -> Iterator[dict]:
    """在后台线程运行 agent.chat()，把过程事件 + 最终结果按序 yield 出来。

    agent 需具备可写属性 event_sink 和方法 chat(str) -> CustomerServiceResponse。
    """
    q: "queue.Queue" = queue.Queue()

    def worker():
        agent.event_sink = q.put
        try:
            result = agent.chat(user_input)
            q.put({"type": "reply", "content": result.reply})
            q.put({
                "type": "metadata",
                "intent": result.intent.value,
                "confidence": result.confidence,
                "requires_human": result.requires_human,
                "follow_up_question": result.follow_up_question,
            })
        except Exception as e:  # noqa: BLE001 —— 流式场景需把异常透传给前端
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
