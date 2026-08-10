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

from app.agent.skills.versioning import VERSION_FILE, fingerprint_skill_dir, read_version

# 单个附带文件注入上下文的字符上限(防一份大文档把上下文顶爆)
MAX_SKILL_FILE_CHARS = 20000


@dataclass
class SkillMeta:
    """Skill 元数据（从 SKILL.md frontmatter 解析）。"""

    name: str
    description: str
    path: Path
    body: str = ""
    workflow: dict = field(default_factory=dict)   # G1 工作流声明(无则空,行为不变)
    #: 这份 skill 服务于谁:"buyer"(默认,C 端客服)/ "seller"(B 端经营与营销)。
    #: frontmatter 不写就是 buyer——既有三份 skill 都不带这个字段,默认值保证它们
    #: 行为逐字节不变;新增卖家侧 skill 必须显式写 `actor: seller`,否则会漏进
    #: 买家会话(见 app/agent/runtime_context.py 里那段说明)。
    actor: str = "buyer"
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

            from app.agent.runtime_context import ACTOR_BUYER, ACTOR_SELLER

            raw_actor = str(meta.get("actor") or ACTOR_BUYER).strip().lower()
            # 非法值按 buyer 处理是**故意**的保守方向:写错一个字(比如 "Seller "
            # 之外的 "b2b")时,后果是这份 skill 只在买家侧可见——那会被发现(店主
            # 问它却说没有这个技能),而反过来"认不出就当卖家 skill"会让一份写歧了
            # 的 skill 悄悄对买家隐身、对店主可见,错误方向相反但更难察觉。
            actor = raw_actor if raw_actor in (ACTOR_BUYER, ACTOR_SELLER) else ACTOR_BUYER

            self._skills[name] = SkillMeta(
                name=name, description=description, path=skill_file,
                workflow=parse_workflow(meta), actor=actor,
            )

    @property
    def skill_count(self) -> int:
        return len(self._skills)

    @property
    def skill_names(self) -> list[str]:
        return list(self._skills.keys())

    def _visible_skills(self, actor: str | None = None) -> list[SkillMeta]:
        """当前 actor 能看见的 skill。

        actor=None 时从 runtime_context 取(每轮由编排器刷新);取不到按 buyer
        处理——保守默认是"少看见",不是"看见全部"。管理端要看全量时显式传
        actor 或直接读 `_skills`(见 /api/admin/skills)。
        """
        from app.agent.runtime_context import get_current_actor

        who = (actor or get_current_actor())
        return [s for s in self._skills.values() if s.actor == who]

    def get_catalog(self, actor: str | None = None) -> list[dict]:
        """返回 skill catalog（name + description），用于注入 system prompt。

        只含当前 actor 归属的 skill:卖家侧的营销/经营 skill 绝不出现在买家
        会话里(反之亦然)——与买卖两侧工具子集不相交是同一条纪律,只是这条
        以前漏在 skill 这一层。
        """
        return [
            {"name": s.name, "description": s.description}
            for s in self._visible_skills(actor)
        ]

    def get_catalog_all(self) -> list[dict]:
        """**全部** skill 的 catalog,不按 actor 过滤。

        只给管理面板用(`/api/admin/skills`):技能管理页要列出仓库里真实存在的
        每一份 skill 并显示其归属,否则店主在页面上根本看不到自己那些卖家 skill。
        它跑在 HTTP 请求上下文里、没有 actor,若走 `get_catalog()` 会退化成
        "只看买家",页面就会凭空少几行。
        **不要**在任何进 prompt 的路径上用它——那正是这次要堵的洞。
        """
        return [{"name": s.name, "description": s.description, "actor": s.actor}
                for s in self._skills.values()]

    def build_catalog_prompt(self, actor: str | None = None) -> str:
        """构建注入 system prompt 的 skill catalog 文本(只列当前 actor 的 skill)。"""
        visible = self._visible_skills(actor)
        if not self.enabled or not visible:
            return ""

        lines = [
            "\n\n## 可用技能（Skills）",
            "以下是你可以使用的专业技能。当用户请求匹配某个技能的适用场景时，",
            "**你必须先调用 `load_skill` 工具加载该技能的标准流程，再按流程处理**——",
            "这些流程封装了必须遵守的合规步骤（如退货需先验证资格、检索政策后再申请，",
            "查物流需先确认订单再查轨迹），直接调用底层工具会漏掉步骤。首轮即应加载。\n",
        ]

        for skill in visible:
            lines.append(f"- **{skill.name}**：{skill.description}")

        lines.append("\n### 技能使用方式")
        lines.append("1. 判断用户问题是否匹配某个技能的描述(看适用关键词)")
        lines.append('2. 匹配则**先** `load_skill(skill_name="技能名")` 加载完整流程,再做具体操作')
        lines.append("3. 按加载的指令流程处理,使用已有工具完成每一步")
        lines.append("4. 仅当不匹配任何技能时,才照常直接回答/调工具")

        return "\n".join(lines)

    def list_skill_files(self, skill_name: str, root: Path | None = None) -> list[str]:
        """该技能目录下除自身 SKILL.md 外的附带文件(相对路径,已排序)。

        Agent Skills 标准里一个技能是**目录**,可带参考资料;这里只做列举,
        内容由模型经 read_skill_file 按需读取(渐进式披露,不一次性灌进上下文)。
        未知技能/目录读不了 → []。

        `root` 不传时由 `_resolve_root` 决定用**候选目录还是正式目录**:灰度期
        必须与 load_skill 拿到的正文同源,否则模型会拿到候选的指令 + 线上的文件
        清单(候选新增的附件一读就是「文件不存在」)。load_skill 会把自己已解析
        出的 root 传进来,保证同一次加载内三处解析结果完全一致。

        安全:rglob 会跟进**符号链接目录**,故必须逐个确认解析后仍在该根目录内。
        否则技能目录里放一个指向别处的符链,就能把外部文件名列进模型上下文——
        内容虽有 read_skill_file 的独立拦截,但文件名本身已是信息泄露。
        """
        if root is None:
            root, _ = self._resolve_root(skill_name)
        if root is None:
            return []
        try:
            root_resolved = root.resolve()
            found: list[str] = []
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(root)
                # 只排除技能自身的顶层 SKILL.md;嵌套的同名文件是正常附带资料
                if rel.as_posix() == "SKILL.md":
                    continue
                # 版本号是内部归因用的元数据,不是给模型看的参考资料——不列出
                # (同样只排除顶层的 .version,理由与上面 SKILL.md 一致)
                if rel.as_posix() == VERSION_FILE:
                    continue
                try:
                    p.resolve().relative_to(root_resolved)
                except ValueError:
                    continue          # 经符链逃出技能目录,不列出
                found.append(rel.as_posix())
            return sorted(found)
        except OSError:
            return []

    def read_skill_file(self, skill_name: str, rel_path: str,
                        root: Path | None = None) -> dict:
        """读取该技能目录下的一个附带文件(供 read_skill_file 工具调用)。

        安全:rel_path 来自**模型输出**,故必须防目录穿越——解析后必须仍在**解析
        出的那个根目录**内(灰度期是候选目录,平时是正式目录),且拒绝绝对路径。
        只按文本读取,**绝不执行**任何内容。
        SKILL.md 不走这里(它由 load_skill 提供,避免重复灌上下文)。

        根目录跟随 `_resolve_root`,与 load_skill / list_skill_files 同源:模型看到
        的清单来自哪个版本,读到的内容就必须来自同一个版本。
        """
        if skill_name not in self._skills:
            return {"success": False, "error": f"未找到技能「{skill_name}」"}

        rel = (rel_path or "").strip()
        if not rel:
            return {"success": False, "error": "文件路径为空"}
        if Path(rel).is_absolute():
            return {"success": False, "error": "只接受技能目录内的相对路径"}
        if Path(rel).name == "SKILL.md":
            return {"success": False, "error": "SKILL.md 已随技能加载,无需再读"}
        if rel == VERSION_FILE:
            return {"success": False, "error": "版本号是内部归因元数据,不是参考资料"}

        if root is None:
            root, _ = self._resolve_root(skill_name)
        if root is None:
            return {"success": False, "error": f"未找到技能「{skill_name}」"}
        try:
            root = root.resolve()
            target = (root / rel).resolve()
            target.relative_to(root)          # 逃出技能目录 → ValueError
        except (OSError, ValueError):
            return {"success": False, "error": "路径越出技能目录,已拒绝"}
        if not target.is_file():
            return {"success": False, "error": f"文件不存在: {rel}"}

        try:
            raw = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"success": False, "error": f"读取失败: {exc}"}

        truncated = len(raw) > MAX_SKILL_FILE_CHARS
        return {"success": True, "skill_name": skill_name, "file": rel,
                "content": raw[:MAX_SKILL_FILE_CHARS], "truncated": truncated}

    def get_workflow(self, skill_name: str) -> dict:
        """该 skill 的工作流声明(无声明/未知 skill → {},即无约束)。

        返回**深拷贝**:声明里 guards 是"列表套字典"、slots 是"字典套字典"的嵌套结构,
        浅拷贝会让调用方顺手改到 SkillMeta 里的活声明——守卫被悄悄改写或清空就形同
        虚设(这些声明守的是退款等动钱工具),故必须深拷。
        """
        skill = self._skills.get(skill_name)
        return copy.deepcopy(skill.workflow) if skill else {}

    def _canary_dir(self, skill_name: str) -> Path | None:
        """灰度路由:该 skill 有活跃候选且当前会话落桶 → 返回**候选目录**,否则 None。

        返回目录而不是正文:技能是**目录**,正文与附带资料必须同源切换。只换正文
        会让灰度会话拿到候选的指令 + 线上的文件清单,候选新增的附件一读就是
        「文件不存在」—— A/B 成绩被这条接线人为压低,看门狗于是把一个好候选回滚掉。

        fail-soft:开关关闭、取不到会话、DB 异常、候选目录/文件缺失都返回 None
        (退回正式版)。灰度是增强,绝不能因为它让 load_skill 失败。
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
            path = Path(canary["candidate_path"])       # 库里记的是候选 SKILL.md
            if not path.is_file():
                return None
            return path.parent
        except Exception:  # noqa: BLE001 灰度任何异常都退回正式版
            return None

    def _resolve_root(self, skill_name: str) -> tuple[Path | None, str]:
        """本次调用该用哪个技能根目录 + 对应 variant。未知技能 → (None, live)。

        load_skill / list_skill_files / read_skill_file **必须**都经这里取根目录:
        分桶是按 session 哈希的确定性函数,同一会话三处解析结果一致,模型看到的
        指令、文件清单与文件内容因而永远来自同一个版本。
        """
        from app.agent.skills.canary import VARIANT_CANARY, VARIANT_LIVE

        skill = self._skills.get(skill_name)
        if skill is None:
            return None, VARIANT_LIVE
        candidate_dir = self._canary_dir(skill_name)
        if candidate_dir is not None:
            return candidate_dir, VARIANT_CANARY
        return skill.path.parent, VARIANT_LIVE

    def load_skill(self, skill_name: str) -> dict:
        """加载指定 skill 的完整指令。供 load_skill 工具调用。

        返回值带 variant(live/canary):灰度期本会话拿到的是哪个版本,由调用方
        记进执行轨迹,供看门狗做 A/B 判定。
        """
        if not self.enabled:
            return {"success": False, "error": "技能系统未启用"}

        from app.agent.runtime_context import get_current_actor

        # 跨 actor 的加载与"不存在"回同一句话,且可用清单也只列当前 actor 的。
        # 刻意不回"这是卖家技能,你无权加载"——那等于告诉买家侧会话"本店有一套
        # 营销技能",技能名本身就是信息泄露(与 ensure_active 对别人的会话按
        # 未知处理、不回 403 是同一条口径:零信息泄露)。
        # 目录里不列 ≠ 点名要不到:模型可能从历史消息、注入文本里拿到技能名,
        # 所以过滤必须落在这里,而不是只靠 build_catalog_prompt 少列一行。
        who = get_current_actor()
        skill = self._skills.get(skill_name)
        if skill is not None and skill.actor != who:
            skill = None
        if not skill:
            available = ", ".join(s.name for s in self._visible_skills(who)) or "无"
            return {
                "success": False,
                "error": f"未找到技能「{skill_name}」，可用技能：{available}",
            }

        from app.agent.skills.canary import VARIANT_CANARY, VARIANT_LIVE

        # 灰度期整目录切换:正文、附带文件清单、按需读取三者都以 root 为准
        root, variant = self._resolve_root(skill_name)
        body: str | None = None
        if variant == VARIANT_CANARY and root is not None:
            try:
                body = _parse_body((root / "SKILL.md").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                # fail-soft:候选正文这一刻读不出来就整体退回正式版,不能半灰半正
                root, variant, body = skill.path.parent, VARIANT_LIVE, None
        if body is None:
            body = skill.load_body()

        # G1:把硬约束附在指令后,让模型事先知道(而不是被拦回才发现),省一轮往返
        # 灰度期约束声明恒用 LIVE skill 的 workflow(而非候选的):候选只替换指令
        # 正文,声明(guards/slots)**不在灰度覆盖范围内**。
        # 注意这是一条真实边界,别误以为被风险分级兜住了:只有当声明里出现动钱工具
        # 时才会判高危转人工;若候选只是改了只读 skill 的 guards/slots,它会被判低危
        # 并走自动灰度,而灰度**从未实际执行过**那份新声明(强制与约束文案都用 live 版)。
        # 也就是说:声明变更的效果不会被灰度验证到,转正后才首次生效。
        from app.agent.skills.workflow import render_constraints

        # 渐进式披露:只告知有哪些附带资料可读,不把内容灌进来(要用时模型自己调工具取)
        # 传 root:清单必须与上面那份正文出自同一个版本(灰度期即候选目录)
        files = self.list_skill_files(skill_name, root=root)
        files_block = ""
        if files:
            files_block = (
                "\n\n## 本技能附带的参考资料(按需读取,不必全读)\n"
                + "\n".join(f"- {f}" for f in files)
                + f"\n需要某份资料时调用 `read_skill_file(skill_name=\"{skill.name}\", "
                  "file=\"上面的相对路径\")` 取内容。\n"
            )

        # 版本取**本次实际服务的 root**:灰度期是候选目录,平时是正式目录——
        # 与上面正文/文件清单同源,保证"加载那一刻"的版本与实际执行的树一致。
        actual_root = root if root is not None else skill.path.parent
        version = read_version(actual_root)
        # 内容指纹同样取自 actual_root:候选目录从不带 .version(见 versioning.py
        # 顶部说明),整数版本号在灰度期因此永远读到"1",无法区分两批不同候选——
        # 指纹不依赖任何人在候选创建时打标,直接对被服务的这棵树现算,天然覆盖
        # live/candidate 两种情况,且不同内容绝不会算出同一个指纹。
        fingerprint = fingerprint_skill_dir(actual_root)

        return {
            "success": True,
            "skill_name": skill.name,
            "instructions": body + render_constraints(skill.workflow) + files_block,
            "variant": variant,
            "version": version,
            "skill_fingerprint": fingerprint,
        }
