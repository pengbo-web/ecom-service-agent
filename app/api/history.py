"""把 raw_messages 重建为可显示的对话气泡:用户消息 + 每轮最终回复。

跳过中间思考、工具调用/结果(role=tool、带 tool_calls 的 assistant),
只保留 user 与该轮的最终回复——即前端能直接渲染的历史气泡。

**买卖两侧的落盘格式不一样,所以要两种模式。**

- 买家侧:最终回复由 `_extract_structured_response` 产出,落盘是一个 JSON
  串 `{"reply": ..., "intent": ...}`;中间思考是**纯文本** assistant 消息。
  所以"能 json.loads 出带 reply 的 dict"正好把最终回复挑出来。
- 卖家侧(参谋/增长):落盘的最终回复就是**纯文本**,没有那层结构化包装。

拿买家侧那条判据去解析参谋会话,结果是**每一条回复都被当成"中间思考"跳过**——
实测参谋会话里 4 条消息只重建出 1 个气泡(只剩用户那句),店主刷新页面看到的是
"参谋一句话都没说过"。而数据其实好好地存在 `app/sessions/seller/<sid>.json` 里。
"""

import json


def reconstruct_bubbles(messages: list, structured: bool = True) -> list[dict]:
    """重建气泡。`structured=True` 走买家侧口径(默认,行为逐字不变)。

    `structured=False` 给卖家侧:最终回复是纯文本,判据换成"**该轮最后一条
    不带 tool_calls 的 assistant 消息**"。取最后一条而不是第一条,是因为
    ReAct 循环中途也可能落下纯文本 assistant;一轮里真正要显示的是收尾那条。
    """
    if not structured:
        return _plain_text_bubbles(messages)

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


def _unwrap(content: str) -> str:
    """内容是 `{"reply": ...}` 那种结构化包装就拆出来,否则原样返回。

    **同一条参谋会话里两种格式是并存的**:多数回复是纯文本,但经
    `_append_agent_reply` 之类路径写进去的仍是结构化 JSON。不拆的话,页面上会
    直接渲染出 `{"intent":"other","confidence":0.5,"reply":"样本量较小…"}`
    这一整串给店主看(实测就是这样)。
    """
    stripped = (content or "").lstrip()
    if not stripped.startswith("{"):
        return content
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return content
    if isinstance(data, dict) and isinstance(data.get("reply"), str):
        return data["reply"]
    return content


def _plain_text_bubbles(messages: list) -> list[dict]:
    """卖家侧:每轮取最后一条 assistant 作为回复,内容按需拆包。

    `pending` 而不是即时 append:一轮里可能有多条纯文本 assistant(中途思考 +
    收尾回复),后来的覆盖先前的,遇到下一条 user 消息或消息走完时才落定。
    这样气泡顺序天然是 user → assistant → user → assistant。
    """
    bubbles: list[dict] = []
    pending: str | None = None

    def flush() -> None:
        nonlocal pending
        if pending:
            bubbles.append({"role": "assistant", "content": pending})
        pending = None

    for m in messages or []:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role == "user":
            flush()                       # 上一轮的回复先落定,再放这一轮的提问
            bubbles.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant" and not m.get("tool_calls") and content:
            pending = _unwrap(content)
    flush()
    return bubbles
