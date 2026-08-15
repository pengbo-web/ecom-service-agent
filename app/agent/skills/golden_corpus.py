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

from prompts import get as _get_prompt

GOLDEN_SYSTEM_PROMPT = _get_prompt("skills/golden_corpus")


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
