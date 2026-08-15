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

H3.3 追加 `improve_skill`：针对已有 skill 的失败会话样本（转人工/低分），让 LLM
在现有 SKILL.md 基础上产出改进版，同样只写候选（`out_dir/<name>/SKILL.md`），
绝不覆盖 `definitions/` 正式目录，人工确认后才替换生效。失败案例采集（trace 低分 /
HITL 升级且当轮 load 过该 skill）不在本文件实现，由 H3.5 闭环入口拼装后传入。
H3.5 复用本文件全部函数。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.agent.skills.clustering import cluster_texts
from app.agent.skills.validator import is_safe_skill_name, known_tool_names, validate_candidate

logger = logging.getLogger(__name__)

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

from prompts import get as _get_prompt

# 尾部换行是**拼接分隔符**,不是三引号写法的副产品:本 prompt 会与
# `build_tool_hint()`(以换行开头)相接,少了它工具清单会紧贴在最后一句下面。
# 提示词加载器对每个 .md 做 strip(),所以分隔符必须由这里补——`.md` 文件末尾
# 有没有空行并不可靠(实测 5 个文件里 3 个没有)。
SYNTH_SYSTEM_PROMPT = _get_prompt("skills/synthesis") + "\n"

def build_tool_hint(known: set[str] | None = None) -> str:
    """把工具清单拼成 prompt 片段,防 LLM 凭空编工具名(实测编过 order_list)。

    默认清单来自 `known_tool_names()`——已经是买家客服 Agent 可调的子集
    (卖家专属工具已被排除),不会诱导 LLM 写出买家 Agent 根本调不到的工具名。
    """
    names = sorted(known if known is not None else known_tool_names())
    return (
        "\n可用工具清单(**只能使用**下列工具名,禁止编造其它工具):\n"
        + "\n".join(f"- {n}" for n in names)
        + "\n引用工具时用反引号包裹,例如 `query_order`。\n"
    )


IMPROVE_SYSTEM_PROMPT = _get_prompt("skills/improve") + "\n"


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
    """把归档会话按**语义**聚类;embedding 不可用时回落关键词粗聚类。

    每条 archived 样本须含 `messages`（已是 list——调用方负责 json.loads）。

    为什么从关键词换成语义(见 app/agent/skills/clustering.py 的完整理由):
    原来那张 `INTENT_KEYWORDS` 只有四个桶、先中先得、只看首条 user 消息。测试
    阶段样本少时看不出问题,**生产上"鞋子穿着挤脚想换大一码"这类根本落不进
    任何桶,或者落错桶**——而落桶结果直接决定合成出什么 skill,输入端的失真
    会一路传到产出。

    **关键词聚类保留为兜底,不是删掉**:embedding 端点故障 / 维度不匹配 /
    没配 key 时仍能跑完整条合成流程。自进化是离线增强,不该因为向量这一步挂了
    就整个跑不动。回落时记 warning,不静默——否则"为什么这次聚类结果变差了"
    会查不出来。
    """
    if not archived:
        return {}

    texts = [_first_user_message(item.get("messages") or []) for item in archived]
    clusters = cluster_texts(texts)
    if clusters is None:
        logger.warning("语义聚类不可用,本次回落关键词聚类(合成质量可能下降)")
        groups: dict[str, list[dict]] = {}
        for item in archived:
            label = _classify(_first_user_message(item.get("messages") or []))
            groups.setdefault(label, []).append(item)
        return groups

    # 簇标签仍用关键词分类器给一个**可读**的名字(它决定产出文件名的一部分),
    # 但**分组本身已由语义决定**——关键词在这里只负责命名,不再负责归类。
    # 同一关键词命中多个语义簇时加序号,避免两个不同意图的簇撞同一个名字。
    out: dict[str, list[dict]] = {}
    used: dict[str, int] = {}
    for idx in clusters:
        members = [archived[i] for i in idx]
        base = _classify(texts[idx[0]])
        used[base] = used.get(base, 0) + 1
        label = base if used[base] == 1 else f"{base}-{used[base]}"
        out[label] = members
    return out


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


def synthesize_one(client, model: str, group: list[dict],
                   system_prompt: str = SYNTH_SYSTEM_PROMPT,
                   known_tools: set[str] | None = None) -> dict | None:
    """LLM 从同类样本归纳出一个候选 skill。

    - system_prompt 可替换(金牌客服蒸馏用不同的归纳指令)。
    - 真实工具清单注入 system prompt;产物再过 validate_candidate 兜底——
      坏 frontmatter 或引用未知工具 → 返回 None,调用方跳过不崩。
    """
    known = known_tools if known_tools is not None else known_tool_names()
    prompt = _build_prompt(group)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt + build_tool_hint(known)},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or ""

    report = validate_candidate(content, known=known)
    if not report["valid"]:
        return None

    return {"name": report["name"], "content": content}


def synthesize_skills(client, model: str, samples: list[dict], out_dir: str,
                      system_prompt: str = SYNTH_SYSTEM_PROMPT,
                      known_tools: set[str] | None = None,
                      min_group_size: int = MIN_GROUP_SIZE) -> list[Path]:
    """聚类 + 逐组合成候选 skill，写入 out_dir/<name>/SKILL.md。

    - 空样本 → []，不写文件。
    - 样本数 < min_group_size 的组跳过(默认 MIN_GROUP_SIZE，即单例不成"重复模式")。
    - 坏输出/引用未知工具的组跳过(fail-soft),不影响其他组。
    """
    if not samples:
        return []

    known = known_tools if known_tools is not None else known_tool_names()
    out_root = Path(out_dir)
    written: list[Path] = []

    for _label, group in group_samples(samples).items():
        if len(group) < min_group_size:
            continue

        result = synthesize_one(client, model, group,
                               system_prompt=system_prompt, known_tools=known)
        if result is None:
            continue

        # 兜底:写盘前再确认目录名安全(名字来自 LLM 产物,不能只依赖上游校验)
        if not is_safe_skill_name(result["name"]):
            continue

        skill_dir = out_root / result["name"]
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(result["content"], encoding="utf-8")
        written.append(skill_file)

    return written


def _build_improve_prompt(skill: dict, failure_cases: list[dict]) -> str:
    lines = [
        "现有 SKILL.md 全文：",
        "```",
        str(skill.get("content") or ""),
        "```",
        "",
        "以下是该 skill 处理失败（转人工/低分）的历史会话样本（已截断）：",
        "",
    ]
    for i, sample in enumerate((_truncate_sample(s) for s in failure_cases), start=1):
        lines.append(f"## 失败案例 {i}")
        if sample.get("summary"):
            lines.append(f"摘要：{sample['summary']}")
        for m in sample["messages"]:
            lines.append(f"- {m.get('role')}: {m.get('content')}")
        lines.append("")
    lines.append("请基于以上失败案例改进这份 SKILL.md（只输出改进后的完整文本）。")
    return "\n".join(lines)


def improve_skill(
    client, model: str, skill: dict, failure_cases: list[dict], out_dir: str,
    known_tools: set[str] | None = None,
) -> Path | None:
    """针对失败会话样本改进已有 skill，产出候选（不自动生效）。

    - `skill`：`{"name": str, "content": <现 SKILL.md 全文>}`，由调用方从
      definitions 读出。
    - `failure_cases`：与该 skill 相关的失败会话样本（转人工/低分），形状同
      归档 dict（`messages` list / `summary`）。失败案例采集本身（trace 低分
      / HITL 升级且当轮 load 过该 skill）不在本函数实现，H3.5 的闭环入口
      会拼装好样本后传入。
    - 空失败样本 → 返回 None，不改、不调 LLM。
    - LLM 输出经 `validate_candidate` 校验（frontmatter 完整性 + 工具名真实存在）；
      未通过（坏输出）→ 返回 None，不崩、不写文件。
    - 产物写 `out_dir/<name>/SKILL.md`（与 H3.1 候选同名则覆盖，均为候选，
      以新为准），返回写出的文件路径。
    """
    if not failure_cases:
        return None

    known = known_tools if known_tools is not None else known_tool_names()
    prompt = _build_improve_prompt(skill, failure_cases)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": IMPROVE_SYSTEM_PROMPT + build_tool_hint(known)},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or ""

    report = validate_candidate(content, known=known)
    if not report["valid"]:
        return None
    name = report["name"]
    # name 一致性兜底:LLM 意外改名会让候选目录漂移,甚至静默覆盖其他候选——按坏输出丢弃。
    if name != str(skill.get("name") or "").strip():
        return None

    if not is_safe_skill_name(name):
        return None

    skill_dir = Path(out_dir) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    return skill_file
