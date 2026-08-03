"""G1 Skill 工作流守卫:把 SKILL.md 里的流程文字变成运行时强制的硬约束。

背景:`process-return/SKILL.md` 写着"退款前必须确认订单号和退款原因,不能跳过",
但这只是给模型的建议——模型完全可以直接调 apply_refund。本模块让这类约束可被
声明、可被强制。

只用于高危流程(碰钱/不可逆)。咨询/推荐类 skill 不加声明即完全不受影响
(Agent Skills 标准刻意选择指令式而非 schema 式,僵化 schema 在边缘场景更差)。

与 consent.py 的分工:consent 管**授权**(用户批没批),本模块管**顺序与参数**
(前置步骤做没做、参数合不合法)。两者独立,可同时生效。

fail-open 铁律:声明本身有问题(非 dict、guards 不是 list、正则非法、字段类型
不对)一律放行。守卫是为了拦住模型跳步,不是给自己制造新故障点。
"""

from __future__ import annotations

import re


def parse_workflow(meta: dict | None) -> dict:
    """从 frontmatter dict 取 workflow 声明块;缺失或结构不对 → {}(视为无约束)。"""
    if not isinstance(meta, dict):
        return {}
    block = meta.get("workflow")
    return block if isinstance(block, dict) else {}


def guards_for(workflow: dict, tool: str) -> list[dict]:
    """取针对该工具的守卫声明;结构不对的条目跳过(fail-open)。"""
    guards = workflow.get("guards") if isinstance(workflow, dict) else None
    if not isinstance(guards, list):
        return []
    return [g for g in guards
            if isinstance(g, dict) and g.get("tool") == tool]


def _slot_spec(workflow: dict, field: str) -> dict:
    slots = workflow.get("slots") if isinstance(workflow, dict) else None
    if not isinstance(slots, dict):
        return {}
    spec = slots.get(field)
    return spec if isinstance(spec, dict) else {}


def check_slots(workflow: dict, args: dict, fields: list[str]) -> str | None:
    """校验参数:必须存在且非空;有 pattern/min_length 声明的还要满足。

    slots 里未定义的字段只做非空检查。非法正则视为无约束(fail-open)。
    返回 None = 通过;否则返回可直接转达模型的错误说明(带 hint,便于自纠)。
    """
    if not isinstance(fields, list):
        return None

    for field in fields:
        value = args.get(field) if isinstance(args, dict) else None
        text = "" if value is None else str(value).strip()
        spec = _slot_spec(workflow, field)
        hint = str(spec.get("hint") or "").strip()

        if not text:
            return f"参数 {field} 缺失或为空" + (f"({hint})" if hint else "")

        min_length = spec.get("min_length")
        if isinstance(min_length, int) and len(text) < min_length:
            return (f"参数 {field} 过短(至少 {min_length} 个字符)"
                    + (f":{hint}" if hint else ""))

        pattern = spec.get("pattern")
        if isinstance(pattern, str) and pattern:
            try:
                matched = re.fullmatch(pattern, text) is not None
            except re.error:
                matched = True      # 声明里的正则写坏了 → fail-open
            if not matched:
                return (f"参数 {field} 格式不正确"
                        + (f":{hint}" if hint else f"(应匹配 {pattern})"))
    return None


def check_prerequisites(guard: dict, args: dict, prior_calls: list[dict]) -> str | None:
    """校验前置工具:必须在本轮**成功**调用过,且 same_args 指定的参数值一致。

    被守卫拦下的调用(blocked=True)不算"已成功调用过"。
    """
    required = guard.get("requires_tools")
    if not isinstance(required, list) or not required:
        return None

    same_args = guard.get("same_args")
    same_args = same_args if isinstance(same_args, list) else []

    ok_calls = [c for c in (prior_calls or [])
                if isinstance(c, dict) and c.get("ok") and not c.get("blocked")]

    for tool_name in required:
        matches = [c for c in ok_calls if c.get("name") == tool_name]
        if not matches:
            return f"本轮尚未成功调用前置工具 {tool_name}"

        if same_args:
            for field in same_args:
                want = str((args or {}).get(field) or "").strip()
                got_any = any(
                    str((c.get("args") or {}).get(field) or "").strip() == want
                    for c in matches
                )
                if not got_any:
                    return (f"前置工具 {tool_name} 的 {field} 与本次调用不一致"
                            f"(本次 {field}={want or '空'})")
    return None


def evaluate_guards(workflow: dict, tool: str, args: dict,
                    prior_calls: list[dict]) -> str | None:
    """综合判定该工具本次是否放行。None = 放行;字符串 = 拒绝理由。

    拒绝理由 = 作者写的 deny 文案 + 具体缺失项,便于模型据此补齐而不是瞎猜。
    """
    for guard in guards_for(workflow, tool):
        detail = check_prerequisites(guard, args, prior_calls)
        if detail is None:
            validate = guard.get("validate")
            detail = check_slots(workflow, args,
                                 validate if isinstance(validate, list) else [])
        if detail is not None:
            deny = str(guard.get("deny") or "").strip()
            return f"{deny}。{detail}" if deny else detail
    return None


def render_constraints(workflow: dict) -> str:
    """把守卫渲染成给模型看的文案,附在 skill 指令后。

    让模型**事先**知道约束,而不是靠被拒绝才发现——省一轮往返。
    """
    guards = workflow.get("guards") if isinstance(workflow, dict) else None
    if not isinstance(guards, list):
        return ""

    lines: list[str] = []
    for guard in guards:
        if not isinstance(guard, dict) or not guard.get("tool"):
            continue
        tool = guard["tool"]
        parts: list[str] = []
        required = guard.get("requires_tools")
        if isinstance(required, list) and required:
            parts.append("必须先成功调用 " + "、".join(f"`{t}`" for t in required))
        same_args = guard.get("same_args")
        if isinstance(same_args, list) and same_args:
            parts.append("且 " + "、".join(same_args) + " 需与前置调用一致")
        validate = guard.get("validate")
        if isinstance(validate, list) and validate:
            parts.append("参数 " + "、".join(validate) + " 必须已确认且格式合法")
        if parts:
            lines.append(f"- 调用 `{tool}` 前:" + ";".join(parts) + "。")

    if not lines:
        return ""
    return ("\n\n## 本技能的硬约束(系统强制,违反会被拦回)\n"
            + "\n".join(lines)
            + "\n未满足时工具不会执行,请先补齐上述前置步骤再调用。\n")


def referenced_workflow_tools(workflow: dict) -> set[str]:
    """声明里出现的所有工具名(被守卫的 + 前置的),供候选校验查真实性。"""
    guards = workflow.get("guards") if isinstance(workflow, dict) else None
    if not isinstance(guards, list):
        return set()

    names: set[str] = set()
    for guard in guards:
        if not isinstance(guard, dict):
            continue
        tool = guard.get("tool")
        if isinstance(tool, str) and tool:
            names.add(tool)
        required = guard.get("requires_tools")
        if isinstance(required, list):
            names.update(t for t in required if isinstance(t, str) and t)
    return names
