"""Skill 加载器：扫描 SKILL.md 文件，提供发现（catalog）和激活（load）能力。

遵循 Anthropic Agent Skills 开放标准（agentskills.io/specification）：
- 每个 Skill 是一个目录，包含 SKILL.md 文件（YAML frontmatter + Markdown 指令）
- 启动时只加载 name + description（~100 tokens/skill），注入 system prompt
- Agent 调用 load_skill 工具时，加载完整 SKILL.md body 到上下文
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class SkillMeta:
    """Skill 元数据（从 SKILL.md frontmatter 解析）。"""

    name: str
    description: str
    path: Path
    body: str = ""
    workflow: dict = field(default_factory=dict)   # G1 工作流声明(无则空,行为不变)
    _body_loaded: bool = field(default=False, repr=False)

    def load_body(self) -> str:
        """加载完整的 SKILL.md body（指令部分）。"""
        if not self._body_loaded:
            raw = self.path.read_text(encoding="utf-8")
            self.body = _parse_body(raw)
            self._body_loaded = True
        return self.body


def _parse_frontmatter(content: str) -> dict:
    """解析 YAML frontmatter（yaml.safe_load，支持嵌套/列表/多行/引号）。

    解析失败（非法 YAML / 非映射）一律返回 {}，让单个坏 SKILL.md 被跳过，
    而不是拖垮整个目录扫描或误解析。
    """
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_body(content: str) -> str:
    """提取 frontmatter 之后的 Markdown body。"""
    match = re.match(r"^---\s*\n.*?\n---\s*\n?", content, re.DOTALL)
    if match:
        return content[match.end():].strip()
    return content.strip()


class SkillManager:
    """Skill 管理器：发现、注册、加载 Skills。"""

    def __init__(self, skills_dir: str = "app/agent/skills/definitions", enabled: bool = True):
        self.skills_dir = Path(skills_dir)
        self.enabled = enabled
        self._skills: dict[str, SkillMeta] = {}

        if self.enabled:
            self._discover()

    def _discover(self) -> None:
        """扫描 skills 目录，解析所有 SKILL.md 的 frontmatter。"""
        if not self.skills_dir.exists():
            return

        for skill_dir in sorted(self.skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            content = skill_file.read_text(encoding="utf-8")
            meta = _parse_frontmatter(content)

            name = str(meta.get("name") or "").strip()
            description = str(meta.get("description") or "").strip()
            if not name or not description:
                continue

            from app.agent.skills.workflow import parse_workflow

            self._skills[name] = SkillMeta(
                name=name, description=description, path=skill_file,
                workflow=parse_workflow(meta),
            )

    @property
    def skill_count(self) -> int:
        return len(self._skills)

    @property
    def skill_names(self) -> list[str]:
        return list(self._skills.keys())

    def get_catalog(self) -> list[dict]:
        """返回 skill catalog（name + description），用于注入 system prompt。"""
        return [
            {"name": s.name, "description": s.description}
            for s in self._skills.values()
        ]

    def build_catalog_prompt(self) -> str:
        """构建注入 system prompt 的 skill catalog 文本。"""
        if not self.enabled or not self._skills:
            return ""

        lines = [
            "\n\n## 可用技能（Skills）",
            "以下是你可以使用的专业技能。当用户请求匹配某个技能的适用场景时，",
            "**你必须先调用 `load_skill` 工具加载该技能的标准流程，再按流程处理**——",
            "这些流程封装了必须遵守的合规步骤（如退货需先验证资格、检索政策后再申请，",
            "查物流需先确认订单再查轨迹），直接调用底层工具会漏掉步骤。首轮即应加载。\n",
        ]

        for skill in self._skills.values():
            lines.append(f"- **{skill.name}**：{skill.description}")

        lines.append("\n### 技能使用方式")
        lines.append("1. 判断用户问题是否匹配某个技能的描述(看适用关键词)")
        lines.append('2. 匹配则**先** `load_skill(skill_name="技能名")` 加载完整流程,再做具体操作')
        lines.append("3. 按加载的指令流程处理,使用已有工具完成每一步")
        lines.append("4. 仅当不匹配任何技能时,才照常直接回答/调工具")

        return "\n".join(lines)

    def get_workflow(self, skill_name: str) -> dict:
        """该 skill 的工作流声明(无声明/未知 skill → {},即无约束)。"""
        skill = self._skills.get(skill_name)
        return dict(skill.workflow) if skill else {}

    def load_skill(self, skill_name: str) -> dict:
        """加载指定 skill 的完整指令。供 load_skill 工具调用。"""
        if not self.enabled:
            return {"success": False, "error": "技能系统未启用"}

        skill = self._skills.get(skill_name)
        if not skill:
            available = ", ".join(self._skills.keys()) or "无"
            return {
                "success": False,
                "error": f"未找到技能「{skill_name}」，可用技能：{available}",
            }

        body = skill.load_body()

        # G1:把硬约束附在指令后,让模型事先知道(而不是被拦回才发现),省一轮往返
        from app.agent.skills.workflow import render_constraints

        return {
            "success": True,
            "skill_name": skill.name,
            "instructions": body + render_constraints(skill.workflow),
        }
