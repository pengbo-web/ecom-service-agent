"""H3.4 用户建模：从归档会话样本归纳行为偏好标签，写入结构化用户档案（G3）。

设计与 H3.1 `synthesizer.py` 一脉相承：LLM 输出 fail-soft 校验（坏输出不崩，
直接判负跳过）、样本注入 prompt 前做同样的截断（复用 `_truncate_sample`）。

半自动铁律不适用于本模块——标签写档案（`UserProfileStore.add_tag`）是低风险
的追加式操作（去重、无覆盖），可直接落库，不需要人工确认候选。

失败案例/样本来源说明：由调用方（H3.5 闭环入口）从该用户的归档会话
（`session_archive`）中取出后传入，本模块只负责"归纳 + 落库"。
"""

from __future__ import annotations

import json

from app.agent.memory.profile import get_profile_store
from app.agent.skills.synthesizer import _truncate_sample

# 每个标签的最大字数、最多标签数（prompt 要求 LLM 遵守，解析后再兜底裁剪一次）
MAX_TAG_CHARS = 10
MAX_TAGS = 5

MODEL_USER_SYSTEM_PROMPT = """你是电商客服用户行为分析器。

下面会给你某个用户的历史会话样本（已解决、已截断）。请从中归纳出该用户的
行为偏好标签，供客服 Agent 后续对话时参考（如"偏好红色"“常问物流”“价格敏感”）。

严格要求：
- 只输出一个 JSON 数组，形如 ["偏好红色","价格敏感"]，不要任何额外说明、
  不要用 markdown 代码块包裹。
- 每个标签不超过 10 个字，最多输出 5 个标签。
- 数组元素必须是非空字符串，只归纳有依据的偏好，不要编造。
"""


def _build_prompt(samples: list[dict]) -> str:
    lines = ["以下是该用户的历史会话样本（已截断，仅供归纳行为偏好参考）：", ""]
    for i, sample in enumerate((_truncate_sample(s) for s in samples), start=1):
        lines.append(f"## 样本 {i}")
        if sample.get("summary"):
            lines.append(f"摘要：{sample['summary']}")
        for m in sample["messages"]:
            lines.append(f"- {m.get('role')}: {m.get('content')}")
        lines.append("")
    lines.append("请基于以上样本归纳该用户的行为偏好标签（只输出 JSON 数组）。")
    return "\n".join(lines)


def _parse_tags(content: str) -> list[str]:
    """解析 LLM 输出的 JSON 数组；坏输出（非 JSON/非数组/元素非法）→ []。"""
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return []

    if not isinstance(data, list):
        return []

    tags: list[str] = []
    for item in data:
        if not isinstance(item, str):
            continue
        tag = item.strip()
        if not tag:
            continue
        tags.append(tag[:MAX_TAG_CHARS])
        if len(tags) >= MAX_TAGS:
            break
    return tags


def model_user(client, model: str, user_id: str, samples: list[dict], store=None) -> list[str]:
    """从用户的归档会话样本归纳行为偏好标签，写入结构化档案。

    - `samples`：该用户的归档会话样本（形状同 `session_archive`：`messages`
      list / `summary`）。空样本 → 返回 `[]`，不调 LLM、不写库。
    - LLM 输出经 `_parse_tags` 校验（只接受 JSON 数组、元素为非空字符串）；
      坏输出 → 返回 `[]`，不崩、不写库。
    - `store` 为 None 时用 `get_profile_store()`；若门控关闭（返回 None）
      则只返回解析出的标签，不落库、不崩。
    - 有效标签逐个 `store.add_tag(user_id, tag)`（`add_tag` 内部去重）。
    - 返回最终解析出的标签列表（无论是否成功落库）。
    """
    if not samples:
        return []

    prompt = _build_prompt(samples)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": MODEL_USER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or ""

    tags = _parse_tags(content)
    if not tags:
        return []

    target_store = store if store is not None else get_profile_store()
    if target_store is not None:
        for tag in tags:
            target_store.add_tag(user_id, tag)

    return tags
