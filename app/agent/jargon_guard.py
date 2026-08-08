"""L4:出话内部黑话检测——旁路埋点,只做"看见",不阻断、不改写。

背景:实测发现回复里出现过「已加载的「process-return」技能流程」「我也可以先
**调用工具**」这类内部实现词汇——`app/prompts/agents.py` 的 SAFETY_RULES 已经
加了硬规则禁止这么说,但 prompt 规则不保证 100% 生效(本项目历史上已经吃过
"规则写了但没压住"的亏,例如店铺语气与默认风格块的冲突,见
`app/config/shop_profile.py` 的说明)。所以额外加一道**检测**:回复生成完之后
扫一遍,命中就发一条观测事件,让"是否又犯"变得可见——不用等买家投诉才发现。

fail-soft:任何异常都不影响主回复,调用方（`app/agent/chat.py`）负责用
try/except 包裹,这里本身也不抛不可预期的异常。

词表来源单一:skill 名字必须从调用方传入的 `skill_names` 取（其来源应为
`SkillManager.skill_names`，见 `app/agent/skills/loader.py`），本模块不自己
维护、也不重新发现一份 skill 清单——这个仓库已经因为"手抄表跟真源 drift"
出过好几次问题,不要再添一份。
"""

from __future__ import annotations

# 与具体 skill 名单无关的固定内部黑话/字段名词表。
INTERNAL_JARGON_TERMS: list[str] = [
    "技能流程",
    "已加载",
    "调用工具",
    "系统提示",
    "system prompt",
    "load_skill",
    "skill_name",
    "requires_human",
    "confidence",
]


def detect_internal_leak(reply_text: str, skill_names) -> list[str]:
    """扫描回复文本，返回命中的 skill 名 / 内部黑话列表（无命中则为空列表）。

    `skill_names` 由调用方传入（应取自 `SkillManager.skill_names`），本函数
    只负责比对，不负责发现——避免检测器和调用方各自持有一份可能不同步的清单。
    """
    text = reply_text or ""
    if not text:
        return []
    hits: list[str] = []
    vocabulary = list(skill_names or []) + INTERNAL_JARGON_TERMS
    for term in vocabulary:
        if term and term in text:
            hits.append(term)
    return hits
