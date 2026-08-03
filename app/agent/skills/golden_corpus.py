"""G5 金牌客服语料蒸馏:从人工接管过的会话里学人的处理经验。

语料来源是确定的、不需要启发式猜测:坐席在工作台的人工回复由
`app/api/app.py::admin_session_reply` 写成 assistant 消息,内容是 JSON 且
`intent` 固定为 "human_agent"。凡含这种消息的归档会话,就是"AI 没搞定、人救了
场"的优质样本——正是最该被沉淀成 skill 的部分。

复用 `synthesizer.synthesize_skills`,只替换 system prompt(归纳视角不同:
这里要学人工的判断与话术,而不是总结 AI 自己的套路)。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.skills.synthesizer import synthesize_skills

HUMAN_AGENT_INTENT = "human_agent"

# 金牌语料稀少:一次人工救场就值得学,故不套用"同类样本需≥2"的门槛(否则每桶常仅1条→全被跳过)
GOLDEN_MIN_GROUP_SIZE = 1

GOLDEN_SYSTEM_PROMPT = """你是电商客服 Skill 蒸馏器。

下面会给你一组**人工客服接管处理过**的历史会话样本(AI 未能独立解决,由人工
坐席接手并解决)。请从人工的处理方式中归纳出一份可复用的 SKILL.md,让 AI 下次
遇到同类问题时能像这位人工客服一样处理。

归纳重点:
- 人工是怎么判断的(先确认什么、依据什么下结论);
- 人工做了哪些 AI 漏掉的**核对与升级步骤**(补充查询、交叉验证、及时转人工);
- 人工的话术分寸(如何安抚、如何表达歉意)。

授权红线(必须体现在产出的 SKILL.md 里,不得省略):
- 人工做出的让利、补偿、免运费、超常规退款等**酌情决定**属于人工权限,
  **不得**归纳成 AI 可自主执行的步骤;
- 凡涉及金钱或对外承诺的动作,产出的流程必须写明"经用户确认后调用对应工具"
  或"转人工处理",不得写成由 AI 直接给出;
- 不要把某一次的个案让利写成通用规则。

严格要求:
- 只输出一份完整的 SKILL.md 文本,不要任何额外说明、不要用 markdown 代码块包裹。
- 必须以如下格式开头(frontmatter):
---
name: <kebab-case 技能名>
description: <一句话描述适用场景与关键词,供路由匹配>
---
- frontmatter 之后是 Markdown body,写出步骤化流程。
"""


def is_human_handled(archived: dict) -> bool:
    """该归档会话是否被人工接管过(存在 intent=human_agent 的 assistant 消息)。

    assistant 内容非 JSON(旧格式/纯文本)时按"非人工"处理,不误判。
    """
    for msg in archived.get("messages") or []:
        if msg.get("role") != "assistant":
            continue
        try:
            data = json.loads(str(msg.get("content") or ""))
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("intent") == HUMAN_AGENT_INTENT:
            return True
    return False


def extract_golden_samples(archives: list[dict]) -> list[dict]:
    """筛出被人工接管过的归档会话(金牌语料)。"""
    return [a for a in archives if is_human_handled(a)]


def synthesize_from_golden(client, model: str, archives: list[dict], out_dir: str,
                           known_tools: set[str] | None = None) -> list[Path]:
    """从金牌客服语料蒸馏候选 skill;无人工会话则不调 LLM、直接返回 []。"""
    samples = extract_golden_samples(archives)
    if not samples:
        return []
    return synthesize_skills(client, model, samples, out_dir,
                             system_prompt=GOLDEN_SYSTEM_PROMPT, known_tools=known_tools,
                             min_group_size=GOLDEN_MIN_GROUP_SIZE)
