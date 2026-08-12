import json
from typing import Optional

from openai import OpenAI

from app.prompts.summarizer import SUMMARY_PROMPT


def summarize(
    client: OpenAI,
    model: str,
    old_messages: list[dict],
    prev_summary: Optional[str],
) -> str:
    """把老对话（可选地带上上一次 summary）压缩成新的 summary 文本。

    支持 user / assistant / tool 以及含 tool_calls 的 assistant 消息。
    """
    parts: list[str] = []
    if prev_summary:
        parts.append(f"【此前摘要】\n{prev_summary}")

    transcript_lines = []
    for msg in old_messages:
        role = msg.get("role")
        content = msg.get("content") or ""

        if role == "user":
            transcript_lines.append(f"用户：{content}")
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "?")
                    args = func.get("arguments", "{}")
                    transcript_lines.append(f"客服：[调用工具 {name}({args})]")
            if content:
                transcript_lines.append(f"客服：{content}")
        elif role == "tool":
            display = content if len(content) <= 200 else content[:200] + "..."
            transcript_lines.append(f"[工具结果] {display}")

    parts.append("【待压缩对话】\n" + "\n".join(transcript_lines))

    user_content = "\n\n".join(parts)

    response = client.chat.completions.create(
        model=model,
        temperature=0.3,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    # `content` 可能是 None(部分模型/网关在被截断或触发过滤时就这样返回),
    # 直接 `.strip()` 会 AttributeError 并一路冒到 chat() 外面——实测过。
    content = response.choices[0].message.content
    if not content or not content.strip():
        raise ValueError("摘要模型返回空内容")
    return content.strip()


#: 兜底摘要的字符上限。够装下订单号、金额、状态这类关键事实,又不至于让"压缩"变成
#: 没压缩——`history_keep_recent=3` 时被压掉的通常是十几到几十轮。
FALLBACK_SUMMARY_MAX_CHARS = 1500


def fallback_summary(old_messages: list[dict], prev_summary: Optional[str] = None) -> str:
    """**不调模型**的兜底摘要:把老对话原样摘成一段带截断标记的节录。

    `summarize` 依赖一次 LLM 调用,而压缩发生在一个回合的收尾阶段。调用失败时
    (超时、限流、返回空内容)不能让整个回合翻车,但也不能什么都不做——什么都不做
    意味着历史继续涨,下一轮更可能撞上真正的上下文上限。

    所以这里给一个确定性的退路:同样按角色抽成文本行,尾部保留(**保留尾部而不是
    开头**:越近的内容对下一轮越有用),超长就截断并明确标注这是节录而非摘要。
    订单号、金额这类关键事实是短字符串,这样能活下来。

    刻意不做任何"智能"处理:兜底路径必须可预测、零依赖、不会自己再失败一次。
    """
    lines: list[str] = []
    if prev_summary:
        lines.append(f"【此前摘要】\n{prev_summary}")

    transcript: list[str] = []
    for msg in old_messages or []:
        role = msg.get("role")
        content = str(msg.get("content") or "")
        if role == "user":
            transcript.append(f"用户：{content}")
        elif role == "assistant":
            for tc in (msg.get("tool_calls") or []):
                func = tc.get("function", {})
                transcript.append(f"客服：[调用工具 {func.get('name', '?')}]")
            if content:
                transcript.append(f"客服：{content}")
        elif role == "tool":
            transcript.append(f"[工具结果] {content[:120]}")

    body = "\n".join(transcript)
    if len(body) > FALLBACK_SUMMARY_MAX_CHARS:
        body = "…(更早的内容已省略)\n" + body[-FALLBACK_SUMMARY_MAX_CHARS:]
    lines.append("【历史节录】(摘要生成失败，以下为原始对话节录，非摘要)\n" + body)
    return "\n\n".join(lines)
