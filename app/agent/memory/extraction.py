"""LLM 事实提取：从对话中抽取短期/长期记忆事实。

模式与 app/agent/summarizer.py 一致：格式化对话 → 调用 LLM → 解析结果。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from openai import OpenAI

from app.prompts.memory import LTM_EXTRACTION_PROMPT, STM_EXTRACTION_PROMPT

if TYPE_CHECKING:
    from app.agent.memory.long_term import MemoryFact


def _build_transcript(messages: list[dict]) -> str:
    """将消息列表格式化为文本摘要（复用 summarizer 的逻辑）。"""
    lines = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""

        if role == "user":
            lines.append(f"用户：{content}")
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "?")
                    lines.append(f"客服：[调用工具 {name}]")
            if content:
                lines.append(f"客服：{content}")
        elif role == "tool":
            display = content if len(content) <= 200 else content[:200] + "..."
            lines.append(f"[工具结果] {display}")

    return "\n".join(lines)


def extract_short_term_facts(
    client: OpenAI,
    model: str,
    recent_messages: list[dict],
    existing_facts: list[str],
) -> list[str]:
    """从最近对话中提取/更新短期记忆事实。"""
    transcript = _build_transcript(recent_messages)
    if not transcript.strip():
        return existing_facts

    existing_text = "\n".join(f"- {f}" for f in existing_facts) if existing_facts else "（暂无）"
    prompt = STM_EXTRACTION_PROMPT.format(existing_facts=existing_text)

    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": transcript},
        ],
    )
    raw = (response.choices[0].message.content or "").strip()   # 部分兼容模型可能返回 None

    if "无新信息" in raw:
        return existing_facts

    new_facts = [line.strip().lstrip("- ") for line in raw.splitlines() if line.strip()]
    if not new_facts:
        return existing_facts

    merged = list(existing_facts)
    existing_lower = {f.lower() for f in merged}
    for fact in new_facts:
        if fact.lower() not in existing_lower:
            merged.append(fact)
            existing_lower.add(fact.lower())
    return merged


def extract_long_term_facts(
    client: OpenAI,
    model: str,
    messages: list[dict],
    summary: str | None,
    existing_facts: list,
    piggyback_hint: bool = False,
) -> tuple[list, str, str]:
    """从完整会话中提取长期记忆事实 + 交互摘要(+ 可选 skill 缺口笔记)。

    返回 `(facts, interaction_summary, skill_gap_note)`。第三元素恒为字符串:
    `piggyback_hint=False` 或解析不到时是 ""。

    `piggyback_hint`(WS1 生产者③,技术方案 §2,开关默认关):让这次**本来就要
    付的** LLM 调用顺带输出一个可选字段 `skill_gap_note`——零额外 round trip。
    它只是"标记":落 skill_memory_hints 供采样排序与看板报数,不进任何判定。
    """
    from app.agent.memory.long_term import MemoryFact

    parts = []
    if summary:
        parts.append(f"【对话摘要】\n{summary}")

    transcript = _build_transcript(messages)
    if transcript:
        parts.append(f"【对话内容】\n{transcript}")

    if not parts:
        return [], "", ""

    existing_text = (
        "\n".join(f"- [{f.category}] {f.content}" for f in existing_facts)
        if existing_facts
        else "（暂无）"
    )
    prompt = LTM_EXTRACTION_PROMPT.format(existing_ltm=existing_text)

    user_content = "\n\n".join(parts)
    if piggyback_hint:
        # 捎带而非单开一次调用:巩固这条路本来就要付一次 LLM,顺带问一句零边际
        # 成本;问不到/解析不到都当没有,绝不为此多付一次 round trip。
        user_content += (
            "\n\n【附加输出要求】请在同一份 JSON 里增加一个可选字段 "
            "skill_gap_note(不超过 80 字,可省略):若本次会话暴露了客服流程的"
            "缺口或错误说法(例如答错了政策、该查没查),用一句话描述;否则省略该字段。")

    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ],
    )
    raw = (response.choices[0].message.content or "").strip()   # 兼容模型返回 None content

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [], "（提取失败）", ""

    now = datetime.now().isoformat(timespec="seconds")
    new_facts = []
    for item in data.get("facts", []):
        content = item.get("content", "").strip()
        category = item.get("category", "other")
        if content:
            new_facts.append(MemoryFact(
                content=content,
                category=category,
                created_at=now,
            ))

    interaction_summary = data.get("interaction_summary", "")
    skill_gap_note = ""
    if piggyback_hint:
        # 只信字符串、截断兜底:模型多写的不进标记(标记进看板,噪声会稀释信号)。
        note_raw = data.get("skill_gap_note")
        if isinstance(note_raw, str):
            skill_gap_note = note_raw.strip()[:80]
    return new_facts, interaction_summary, skill_gap_note
