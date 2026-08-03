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


def known_tool_names() -> set[str]:
    """registry 中真实注册的工具名全集(含按开关动态追加的工具)。"""
    from app.agent.tools.registry import TOOL_DEFINITIONS

    names = set()
    for item in TOOL_DEFINITIONS:
        name = (item.get("function") or {}).get("name")
        if name:
            names.add(str(name))
    return names


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
