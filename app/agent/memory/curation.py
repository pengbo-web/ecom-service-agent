"""长期记忆策展（Phase 5）：一次 LLM 调用做合并近义 / 就地纠正 / 按重要性淘汰。

替代 `add_facts` 的"精确去重 + FIFO 截断":后者近义重复留存、老而重要被新琐事挤掉。
保守设计:失败(异常/非法 JSON/空结果)一律返回 None,让调用方降级回 add_facts,绝不误清空记忆。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from openai import OpenAI

from app.prompts.memory import LTM_CURATION_PROMPT

if TYPE_CHECKING:
    from app.agent.memory.long_term import MemoryFact


def _strip_code_fence(raw: str) -> str:
    """去掉 ```json ... ``` 包裹,取出中间的 JSON。"""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return raw


def curate_facts(
    client: OpenAI,
    model: str,
    existing_facts: list,
    new_facts: list,
    max_facts: int,
) -> Optional[list]:
    """LLM 策展 existing+new,返回整理后的 MemoryFact 列表；任何失败返回 None(调用方降级)。

    - 命中原有内容的事实保留其 created_at/source_session(不重置年龄)。
    - 空结果视为失败返回 None,避免把记忆整个清空。
    """
    from app.agent.memory.long_term import MemoryFact

    all_facts = list(existing_facts) + list(new_facts)
    if not all_facts:
        return []

    listing = "\n".join(
        f"{i + 1}. [{f.category}] {f.content}" for i, f in enumerate(all_facts)
    )
    prompt = LTM_CURATION_PROMPT.format(max_facts=max_facts, facts=listing)

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": listing},
            ],
        )
        raw = _strip_code_fence(response.choices[0].message.content or "")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001  网络/解析/结构任何异常都降级
        return None

    kept = data.get("facts") if isinstance(data, dict) else data
    if not isinstance(kept, list):
        return None

    by_content = {f.content.strip().lower(): f for f in all_facts}
    now = datetime.now().isoformat(timespec="seconds")
    result: list = []
    seen: set = set()
    for item in kept:
        if isinstance(item, str):
            content, category = item.strip(), "other"
        elif isinstance(item, dict):
            content = (item.get("content") or "").strip()
            category = item.get("category") or "other"
        else:
            continue
        key = content.lower()
        if not content or key in seen:
            continue
        seen.add(key)
        prev = by_content.get(key)
        result.append(MemoryFact(
            content=content,
            category=category,
            created_at=prev.created_at if prev else now,
            source_session=prev.source_session if prev else "",
        ))

    if not result:
        return None
    return result[:max_facts]
