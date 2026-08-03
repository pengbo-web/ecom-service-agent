"""Skill 加载器：扫描 SKILL.md 文件，提供发现（catalog）和激活（load）能力。

遵循 Anthropic Agent Skills 开放标准（agentskills.io/specification）：
- 每个 Skill 是一个目录，包含 SKILL.md 文件（YAML frontmatter + Markdown 指令）
- 启动时只加载 name + description（~100 tokens/skill），注入 system prompt
- Agent 调用 load_skill 工具时，加载完整 SKILL.md body 到上下文
"""

from __future__ import annotations

import copy
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
        """该 skill 的工作流声明(无声明/未知 skill → {},即无约束)。

        返回**深拷贝**:声明里 guards 是"列表套字典"、slots 是"字典套字典"的嵌套结构,
        浅拷贝会让调用方顺手改到 SkillMeta 里的活声明——守卫被悄悄改写或清空就形同
        虚设(这些声明守的是退款等动钱工具),故必须深拷。
        """
        skill = self._skills.get(skill_name)
        return copy.deepcopy(skill.workflow) if skill else {}

    def _canary_body(self, skill_name: str) -> str | None:
        """灰度路由:该 skill 有活跃候选且当前会话落桶 → 返回候选正文,否则 None。

        fail-soft:开关关闭、取不到会话、DB 异常、候选文件缺失都返回 None(退回
        正式版)。灰度是增强,绝不能因为它让 load_skill 失败。
        """
        from app.config.settings import settings

        if not getattr(settings, "skill_canary_enabled", False):
            return None
        try:
            from app.agent.skills.canary import in_canary_bucket
            from app.agent.tools.bargain import get_current_session
            from app.db import get_db

            session_id = get_current_session()
            if not session_id:
                return None
            canary = get_db().get_active_canary(skill_name)
            if not canary:
                return None
            if not in_canary_bucket(skill_name, session_id, int(canary["percent"])):
                return None
            path = Path(canary["candidate_path"])
            if not path.exists():
                return None
            return _parse_body(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 灰度任何异常都退回正式版
            return None

    def load_skill(self, skill_name: str) -> dict:
        """加载指定 skill 的完整指令。供 load_skill 工具调用。

        返回值带 variant(live/canary):灰度期本会话拿到的是哪个版本,由调用方
        记进执行轨迹,供看门狗做 A/B 判定。
        """
        if not self.enabled:
            return {"success": False, "error": "技能系统未启用"}

        skill = self._skills.get(skill_name)
        if not skill:
            available = ", ".join(self._skills.keys()) or "无"
            return {
                "success": False,
                "error": f"未找到技能「{skill_name}」，可用技能：{available}",
            }

        from app.agent.skills.canary import VARIANT_CANARY, VARIANT_LIVE

        body = skill.load_body()
        variant = VARIANT_LIVE
        canary_body = self._canary_body(skill_name)
        if canary_body is not None:
            body = canary_body
            variant = VARIANT_CANARY

        # G1:把硬约束附在指令后,让模型事先知道(而不是被拦回才发现),省一轮往返
        # 灰度期约束声明恒用 LIVE skill 的 workflow(而非候选的):候选只替换指令
        # 正文,声明(guards/slots)不在灰度覆盖范围内——改声明的候选风险分级会被
        # 判到最高档,直接走人工审核,不会流到这条自动灰度路径。
        from app.agent.skills.workflow import render_constraints

        return {
            "success": True,
            "skill_name": skill.name,
            "instructions": body + render_constraints(skill.workflow),
            "variant": variant,
        }
