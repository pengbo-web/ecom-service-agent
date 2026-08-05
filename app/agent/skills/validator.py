"""G6 候选 Skill 静态校验:frontmatter 完整性 + 工具名是否真实存在。

为什么需要:LLM 合成候选时会凭空编工具名(实测产出过 `order_list`、
`coupon_query`,而真实工具是 `list_user_orders`、`query_coupons`)。这类候选一旦
转正,Agent 会照指令调不存在的工具直接失败——所以转正前必须过这一关。

工具引用的识别口径:只认反引号内的 snake_case 标识符(``query_order``)。
业务散文、中文短语、含空格的商品名都不会被误判成工具名。
"""

from __future__ import annotations

import re

from app.agent.skills.loader import _parse_frontmatter

# 反引号内的 snake_case 标识符:小写字母开头,允许下划线与数字
_INLINE_CODE_RE = re.compile(r"`([a-z][a-z0-9_]*)`")

# skill 名必须是单个安全路径段:候选名来自 LLM 生成的 frontmatter(其素材是归档的
# 顾客对话,可被提示注入),含 .. 或路径分隔符会让写入逃出候选目录、覆盖线上 skill。
_SAFE_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_safe_skill_name(skill_name: str) -> bool:
    """skill 名是否为单个安全路径段(全仓库唯一判定,勿再各处另写一份)。"""
    return bool(_SAFE_SKILL_NAME_RE.match(skill_name or ""))


def known_tool_names() -> set[str]:
    """买家客服 Agent 实际可调的工具名集合(含按开关动态追加的工具)。

    Skill 是**买家客服 Agent**加载执行的,它的"工具是否存在"必须以买家侧
    能摸到的工具为准——不是整张 registry。registry 里还登记着仅供卖家侧
    (店铺参谋/营销 Agent)使用的工具(如 `product_diagnostics`),这些工具
    不在任何买家画像的工具集里;若把它们也算作"已知",候选会顺利通过校验、
    转正后却在买家会话里调不到,造成「未知工具」运行时错误。

    `SELLER_ONLY_TOOLS` 由 registry.py 的 `TOOL_TRAITS` 派生,且有穷尽性测试
    (`test_all_tool_map_entries_are_classified`)兜底每个注册工具都打过标签,
    因此"全集减去卖家专属"就是买家可用集,无需在此另抄一份名单。
    """
    from app.agent.tools.registry import SELLER_ONLY_TOOLS, TOOL_DEFINITIONS

    names = set()
    for item in TOOL_DEFINITIONS:
        name = (item.get("function") or {}).get("name")
        if name:
            names.add(str(name))
    return names - SELLER_ONLY_TOOLS


def referenced_tools(content: str) -> set[str]:
    """抽取 SKILL.md 里引用的工具名候选(反引号内的 snake_case 标识符)。

    只取含下划线的、或已知工具名单形态的标识符,避免把 `body`、`sku` 这类
    普通单词当成工具引用。
    """
    refs = set()
    for token in _INLINE_CODE_RE.findall(content or ""):
        if "_" in token:
            refs.add(token)
    return refs


def validate_candidate(content: str, known: set[str] | None = None) -> dict:
    """校验一份候选 SKILL.md 文本。

    返回 `{"valid", "name", "description", "unknown_tools", "errors"}`:
    - frontmatter 缺失/无 name/无 description → valid=False;
    - 引用了 registry 里不存在的工具 → valid=False,unknown_tools 列出(已排序);
    - 全部通过 → valid=True。
    """
    errors: list[str] = []

    meta = _parse_frontmatter(content)
    if not meta:
        errors.append("缺少合法的 YAML frontmatter(必须以 --- 开头并包含 name/description)")
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name:
        errors.append("frontmatter 缺少 name")
    elif not is_safe_skill_name(name):
        errors.append(f"frontmatter 的 name 不是合法的单个路径段: {name!r}")
    if not description:
        errors.append("frontmatter 缺少 description")

    from app.agent.skills.workflow import parse_workflow, referenced_workflow_tools

    known_set = known if known is not None else known_tool_names()
    # 正文引用 + workflow 声明引用一起校验:声明里的错工具会让守卫拦死真实调用,更危险
    referenced = referenced_tools(content) | referenced_workflow_tools(parse_workflow(meta))
    unknown = sorted(referenced - known_set)
    if unknown:
        errors.append(f"引用了未知工具: {', '.join(unknown)}")

    return {
        "valid": not errors,
        "name": name,
        "description": description,
        "unknown_tools": unknown,
        "errors": errors,
    }
