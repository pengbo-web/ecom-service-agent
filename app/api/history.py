"""把 raw_messages 重建为可显示的对话气泡:用户消息 + 每轮最终结构化回复。

跳过中间思考(纯文本 assistant)、工具调用/结果(role=tool、带 tool_calls 的 assistant),
只保留 user 与含 reply 的结构化 assistant——即前端能直接渲染的历史气泡。
"""

import json


def reconstruct_bubbles(messages: list) -> list[dict]:
    bubbles: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "user":
            bubbles.append({"role": "user", "content": content})
        elif role == "assistant" and not m.get("tool_calls"):
            try:
                data = json.loads(content)
            except (ValueError, TypeError):
                continue   # 中间思考纯文本,非最终回复 → 跳过
            if isinstance(data, dict) and "reply" in data:
                bubbles.append({"role": "assistant", "content": data["reply"]})
    return bubbles
