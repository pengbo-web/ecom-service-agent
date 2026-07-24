"""H3 Skill 自动化生成 v1：会话聚类 + LLM 合成候选 SKILL.md（半自动，只产候选，不自动生效）。

流程：
1. `group_samples`：把冷归档会话（R5 `session_archive`）按首条 user 消息命中的
   意图关键词粗聚类（朴素规则，不引入分类模型）。
2. `synthesize_one`：把同类样本喂给 LLM，要求只输出一份完整 SKILL.md 文本；
   用 `loader._parse_frontmatter` 校验产物，解析失败/缺字段直接判负（None），
   坏 LLM 输出不崩、跳过即可（fail-soft）。
3. `synthesize_skills`：遍历分组，样本数 <2 的组跳过（单例不构成"重复模式"），
   逐组合成，写入 `out_dir/<name>/SKILL.md`，返回写出的文件路径列表。

半自动铁律：本模块只写候选文件（默认 `definitions/_candidates/` 下），绝不
触碰 `definitions/` 正式目录、绝不自动生效——SkillManager._discover 只在
`skills_dir` 的直接子目录里找 SKILL.md，`_candidates/<name>/SKILL.md` 比正式
skill 多嵌套一层（`_candidates` 本身不含 SKILL.md），因此不会被误加载
（细节见 app/agent/skills/loader.py::_discover）。

H3.3 会在本文件追加 `improve_skill`；H3.5 复用本文件全部函数。
"""

from __future__ import annotations

from pathlib import Path

from app.agent.skills.loader import _parse_frontmatter

# 粗聚类：会话首条 user 消息命中的意图关键词组（朴素规则，按顺序匹配，先中先得）
INTENT_KEYWORDS: list[tuple[str, list[str]]] = [
    ("refund", ["退款", "退货"]),
    ("logistics", ["物流", "快递", "到哪"]),
    ("invoice", ["发票"]),
    ("bargain", ["优惠", "便宜", "降价"]),
]

# 单例（样本数 <2）的组不构成"重复模式"，跳过不合成
MIN_GROUP_SIZE = 2

# 样本注入 prompt 时的截断上限，防 prompt 爆炸
MAX_MESSAGES_PER_SAMPLE = 10
MAX_CONTENT_CHARS = 200

SYNTH_SYSTEM_PROMPT = """你是电商客服 Skill 合成器。

下面会给你一组同一类意图的历史会话样本（已解决、已截断）。请从中归纳出一份
可复用的 SKILL.md 技能文档，供客服 Agent 后续遇到同类问题时加载使用。

严格要求：
- 只输出一份完整的 SKILL.md 文本，不要任何额外说明、不要用 markdown 代码块包裹。
- 必须以如下格式开头（frontmatter）：
---
name: <kebab-case 技能名，如 refund-fast-track>
description: <一句话描述适用场景与关键词，供路由匹配>
---
- frontmatter 之后是 Markdown body，写出处理该类问题的步骤化流程
  （参考：第一步...第二步...注意事项，风格对齐已有 skill）。
"""


def _first_user_message(messages: list[dict]) -> str:
    for msg in messages or []:
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def _classify(text: str) -> str:
    for label, keywords in INTENT_KEYWORDS:
        if any(kw in text for kw in keywords):
            return label
    return "other"


def group_samples(archived: list[dict]) -> dict[str, list[dict]]:
    """按首条 user 消息命中的意图关键词组粗聚类归档会话。

    每条 archived 样本须含 `messages`（已是 list——调用方负责 json.loads）。
    """
    groups: dict[str, list[dict]] = {}
    for item in archived:
        messages = item.get("messages") or []
        label = _classify(_first_user_message(messages))
        groups.setdefault(label, []).append(item)
    return groups


def _truncate_sample(sample: dict) -> dict:
    messages = (sample.get("messages") or [])[:MAX_MESSAGES_PER_SAMPLE]
    truncated_messages = [
        {"role": m.get("role"), "content": str(m.get("content") or "")[:MAX_CONTENT_CHARS]}
        for m in messages
    ]
    return {"summary": sample.get("summary"), "messages": truncated_messages}


def _build_prompt(group: list[dict]) -> str:
    lines = ["以下是同一类意图的历史会话样本（已截断，仅供归纳模式参考）：", ""]
    for i, sample in enumerate((_truncate_sample(s) for s in group), start=1):
        lines.append(f"## 样本 {i}")
        if sample.get("summary"):
            lines.append(f"摘要：{sample['summary']}")
        for m in sample["messages"]:
            lines.append(f"- {m.get('role')}: {m.get('content')}")
        lines.append("")
    lines.append("请基于以上样本归纳出一份 SKILL.md（只输出这一份文本）。")
    return "\n".join(lines)


def synthesize_one(client, model: str, group: list[dict]) -> dict | None:
    """LLM 从同类样本归纳出一个候选 skill。

    坏 LLM 输出（无法解析出 name/description）→ 返回 None，调用方跳过不崩。
    """
    prompt = _build_prompt(group)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYNTH_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or ""

    meta = _parse_frontmatter(content)
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        return None

    return {"name": name, "content": content}


def synthesize_skills(client, model: str, samples: list[dict], out_dir: str) -> list[Path]:
    """聚类 + 逐组合成候选 skill，写入 out_dir/<name>/SKILL.md。

    - 空样本 → []，不写文件。
    - 样本数 <2 的组跳过（单例不成"重复模式"）。
    - 坏 LLM 输出的组跳过（fail-soft），不影响其他组。
    """
    if not samples:
        return []

    out_root = Path(out_dir)
    written: list[Path] = []

    for _label, group in group_samples(samples).items():
        if len(group) < MIN_GROUP_SIZE:
            continue

        result = synthesize_one(client, model, group)
        if result is None:
            continue

        skill_dir = out_root / result["name"]
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(result["content"], encoding="utf-8")
        written.append(skill_file)

    return written
