# Skill 工作流守卫（G1：高危流程结构化）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给高风险 Skill 加**声明式前置守卫**，把 SKILL.md 里"必须先查单再退款、参数不能跳过"这类目前纯靠模型自觉的文字，变成运行时强制执行的硬约束——不满足就不放行工具调用。

**Architecture:** 在 SKILL.md 的 YAML frontmatter 里增加可选 `workflow:` 声明块（`_parse_frontmatter` 已用 `yaml.safe_load`，**无需改解析代码**）。声明两类约束：`slots`（参数格式校验）与 `guards`（某工具调用前必须已成功调用过哪些前置工具、参数是否一致）。运行时在 `EcomAgent._execute_tool_call` 入口拦截：不满足约束则**不执行工具**，把结构化拒绝当作工具结果回传给模型，模型据此自行补齐前置步骤——既强制了顺序，又不打断对话。

**Tech Stack:** Python 3.11 / PyYAML（已有）/ pytest。不引入任何新第三方依赖。不新增数据表。

## 前置依赖（必读）

本计划**依赖** `docs/superpowers/plans/2026-08-03-self-evolving-skill-loop.md` 的 **Task 1–2 已完成**，具体依赖：
- `app/agent/skills/execution_trace.py` 中的 `SkillTurn` 已存在，且 `chat.py` 已在回合开始创建 `self._skill_turn`、在 `_execute_tool_call` 中调用 `turn.note_tool_call(...)`。
- 本计划会**扩展** `SkillTurn`：`tool_calls` 元素增加 `args` 与 `blocked` 字段（Task 2）。

若那份计划尚未实施，先做它的 Task 1–2，再回到本计划。

## Global Constraints

- **只约束高危工具，不全量改造**：本计划只给「碰钱/不可逆」流程加守卫（`apply_refund` / `cancel_order` / `change_address` / `negotiate_price`）。咨询、推荐、答疑类 skill 保持纯 markdown——Agent Skills 开放标准刻意选择指令式而非 schema 式，僵化 schema 在边缘场景反而更差。
- **`workflow:` 块必须可选**：没有该块的 SKILL.md 行为与现在**完全一致**（`evaluate_guards` 返回 None 即放行）。所有既有 skill 与测试不受影响。
- **守卫是"否决"，与 G2 埋点的"只观察"性质不同**：这是有意的强制。但**声明结构本身出错时必须 fail-open**（非法 YAML、未知字段、坏正则 → 视为无约束放行），绝不能因为一份写坏的声明让工具全线不可用。
- **守卫拦截不得计入失败率**：被守卫拦下的调用记 `blocked=True`，`SkillTurn.outcome()` 不因它判 `tool_error`——否则守卫越有效，灰度成功率越难看，会污染自进化闭环的 A/B 指标。
- **与 `consent.py` 分工不重叠**：`consent` 管**授权**（用户批没批），`workflow` 管**顺序与参数**（前置步骤做没做、参数合不合法）。两者独立、可同时生效，不要把逻辑混进对方。
- **DRY**：工具名真实性校验复用 `validator.known_tool_names()`，不要再写一份工具清单。
- **代码风格**：中文 docstring/注释，与现有文件一致；跨平台用 `pathlib.Path`。
- **测试命令**：`.venv/Scripts/python.exe -m pytest <file> -v`（Git Bash 下可用）。

## 声明格式（本计划的契约）

```yaml
---
name: process-return
description: 当用户要退货、退款、换货时使用……
workflow:
  slots:                          # 参数格式校验
    order_id:
      pattern: '^ORD-\d{8}-[\w-]+$'
      hint: '订单号形如 ORD-20240115-001'
    reason:
      min_length: 2
      hint: '退款原因不能为空'
  guards:                         # 工具调用前置条件
    - tool: apply_refund          # 要守卫的工具
      requires_tools: [query_order]   # 本轮必须已"成功"调用过这些工具
      same_args: [order_id]           # 且这些参数值须与前置调用一致
      validate: [order_id, reason]    # 这些参数须过 slots 校验且非空
      deny: '退款前必须先用 query_order 核对该订单，并与用户确认退款原因'
---
（markdown body 保持不变，仍是给模型的话术与流程指导）
```

## File Structure

**新建**
| 文件 | 职责 |
|---|---|
| `app/agent/skills/workflow.py` | 声明解析 + 守卫判定 + 硬约束文案渲染。纯函数，无 IO、无 settings 依赖 |
| `tests/test_skill_workflow.py` | 守卫判定单测 |
| `tests/test_skill_workflow_wiring.py` | ReAct 接线单测（拦截生效、放行、fail-open） |

**修改**
| 文件 | 改动 |
|---|---|
| `app/agent/skills/execution_trace.py` | `tool_calls` 元素加 `args` / `blocked`；`outcome()` 忽略 blocked；新增 `note_blocked` |
| `app/agent/skills/loader.py` | `SkillMeta` 加 `workflow` 字段；`_discover` 解析；`load_skill` 把硬约束渲染进 instructions |
| `app/agent/chat.py` | `_execute_tool_call` 入口加守卫拦截；`note_tool_call` 传 args |
| `app/agent/skills/validator.py` | 校验 `workflow:` 声明里引用的工具名真实性 |
| `app/agent/skills/definitions/process-return/SKILL.md` | 加真实 `workflow:` 声明 |

---

### Task 1: 工作流声明解析与守卫判定（纯逻辑）

**Files:**
- Create: `app/agent/skills/workflow.py`
- Test: `tests/test_skill_workflow.py`

**Interfaces:**
- Consumes: 无（纯函数，不依赖项目其它模块）
- Produces:
  - `parse_workflow(meta: dict | None) -> dict`（从 frontmatter dict 取 `workflow` 块；非 dict/缺失/None → `{}`）
  - `guards_for(workflow: dict, tool: str) -> list[dict]`
  - `check_slots(workflow: dict, args: dict, fields: list[str]) -> str | None`
  - `check_prerequisites(guard: dict, args: dict, prior_calls: list[dict]) -> str | None`
  - `evaluate_guards(workflow: dict, tool: str, args: dict, prior_calls: list[dict]) -> str | None`
    返回 `None` = 放行；返回字符串 = 拒绝理由（已含 `deny` 文案与具体缺失项）
  - `render_constraints(workflow: dict) -> str`（把约束渲染成给模型看的文案）
  - `referenced_workflow_tools(workflow: dict) -> set[str]`（供 Task 4 校验工具名）
  - `prior_calls` 元素约定形状：`{"name": str, "ok": bool, "args": dict}`

> **fail-open 原则**：声明本身有问题（不是 dict、guards 不是 list、正则非法、字段类型不对）一律**放行**。守卫的价值是拦住模型跳步，不是给自己制造新的故障点。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_workflow.py`：

```python
"""G1 工作流守卫纯逻辑:声明解析 + 前置条件/参数校验判定。

守卫是"否决"(不满足就不放行工具),但声明本身有问题时必须 fail-open——
一份写坏的声明不该让工具全线不可用。
"""

from app.agent.skills.workflow import (
    check_prerequisites,
    check_slots,
    evaluate_guards,
    guards_for,
    parse_workflow,
    referenced_workflow_tools,
    render_constraints,
)

WORKFLOW = {
    "slots": {
        "order_id": {"pattern": r"^ORD-\d{8}-[\w-]+$", "hint": "订单号形如 ORD-20240115-001"},
        "reason": {"min_length": 2, "hint": "退款原因不能为空"},
    },
    "guards": [
        {
            "tool": "apply_refund",
            "requires_tools": ["query_order"],
            "same_args": ["order_id"],
            "validate": ["order_id", "reason"],
            "deny": "退款前必须先用 query_order 核对该订单",
        }
    ],
}

OK_ORDER_CALL = {"name": "query_order", "ok": True, "args": {"order_id": "ORD-20240115-001"}}
GOOD_ARGS = {"order_id": "ORD-20240115-001", "reason": "尺码不合适"}


# ---------- parse_workflow ----------

def test_parse_workflow_extracts_block():
    assert parse_workflow({"name": "x", "workflow": WORKFLOW}) == WORKFLOW


def test_parse_workflow_missing_or_bad_returns_empty():
    assert parse_workflow({"name": "x"}) == {}
    assert parse_workflow({"workflow": "不是字典"}) == {}
    assert parse_workflow({"workflow": None}) == {}
    assert parse_workflow(None) == {}


# ---------- guards_for ----------

def test_guards_for_matches_tool():
    assert len(guards_for(WORKFLOW, "apply_refund")) == 1
    assert guards_for(WORKFLOW, "query_order") == []


def test_guards_for_tolerates_bad_guards_shape():
    assert guards_for({"guards": "坏结构"}, "apply_refund") == []
    assert guards_for({"guards": ["不是字典"]}, "apply_refund") == []
    assert guards_for({}, "apply_refund") == []


# ---------- check_slots ----------

def test_check_slots_passes_valid_args():
    assert check_slots(WORKFLOW, GOOD_ARGS, ["order_id", "reason"]) is None


def test_check_slots_rejects_pattern_mismatch():
    msg = check_slots(WORKFLOW, {"order_id": "12345", "reason": "不要了"}, ["order_id", "reason"])
    assert msg is not None
    assert "order_id" in msg
    assert "ORD-20240115-001" in msg   # hint 带出去,便于模型自纠


def test_check_slots_rejects_missing_field():
    msg = check_slots(WORKFLOW, {"order_id": "ORD-20240115-001"}, ["order_id", "reason"])
    assert msg is not None
    assert "reason" in msg


def test_check_slots_rejects_too_short():
    msg = check_slots(WORKFLOW, {"order_id": "ORD-20240115-001", "reason": "x"},
                      ["order_id", "reason"])
    assert msg is not None
    assert "reason" in msg


def test_check_slots_unknown_field_is_only_presence_checked():
    """slots 里没定义的字段只检查非空,不报未知字段错。"""
    assert check_slots(WORKFLOW, {"note": "有值"}, ["note"]) is None
    assert check_slots(WORKFLOW, {"note": ""}, ["note"]) is not None


def test_check_slots_bad_regex_fails_open():
    bad = {"slots": {"order_id": {"pattern": "([unclosed"}}}
    assert check_slots(bad, {"order_id": "任意值"}, ["order_id"]) is None


# ---------- check_prerequisites ----------

def test_prerequisites_pass_when_prior_call_succeeded_with_same_args():
    guard = WORKFLOW["guards"][0]
    assert check_prerequisites(guard, GOOD_ARGS, [OK_ORDER_CALL]) is None


def test_prerequisites_reject_when_prior_tool_never_called():
    guard = WORKFLOW["guards"][0]
    msg = check_prerequisites(guard, GOOD_ARGS, [])
    assert msg is not None
    assert "query_order" in msg


def test_prerequisites_reject_when_prior_call_failed():
    guard = WORKFLOW["guards"][0]
    failed = {"name": "query_order", "ok": False, "args": {"order_id": "ORD-20240115-001"}}
    msg = check_prerequisites(guard, GOOD_ARGS, [failed])
    assert msg is not None
    assert "query_order" in msg


def test_prerequisites_reject_when_args_mismatch():
    """查的是 A 单,退的是 B 单 → 必须拦住(这是最危险的跳步)。"""
    guard = WORKFLOW["guards"][0]
    other = {"name": "query_order", "ok": True, "args": {"order_id": "ORD-20991231-999"}}
    msg = check_prerequisites(guard, GOOD_ARGS, [other])
    assert msg is not None
    assert "order_id" in msg


def test_prerequisites_ignore_blocked_prior_calls():
    """被守卫拦下的调用不算"已成功调用过"。"""
    guard = WORKFLOW["guards"][0]
    blocked = {"name": "query_order", "ok": False, "blocked": True,
               "args": {"order_id": "ORD-20240115-001"}}
    assert check_prerequisites(guard, GOOD_ARGS, [blocked]) is not None


# ---------- evaluate_guards ----------

def test_evaluate_allows_unguarded_tool():
    assert evaluate_guards(WORKFLOW, "query_product", {"keyword": "鞋"}, []) is None


def test_evaluate_allows_when_all_conditions_met():
    assert evaluate_guards(WORKFLOW, "apply_refund", GOOD_ARGS, [OK_ORDER_CALL]) is None


def test_evaluate_denies_with_deny_message_and_detail():
    msg = evaluate_guards(WORKFLOW, "apply_refund", GOOD_ARGS, [])
    assert msg is not None
    assert "退款前必须先用 query_order 核对该订单" in msg   # 作者写的 deny 文案
    assert "query_order" in msg                              # 具体缺什么


def test_evaluate_denies_on_bad_args_even_with_prerequisite():
    msg = evaluate_guards(WORKFLOW, "apply_refund",
                          {"order_id": "ORD-20240115-001", "reason": ""}, [OK_ORDER_CALL])
    assert msg is not None
    assert "reason" in msg


def test_evaluate_empty_workflow_always_allows():
    """没有 workflow 声明的 skill 行为与现在完全一致。"""
    assert evaluate_guards({}, "apply_refund", {}, []) is None


# ---------- render_constraints ----------

def test_render_constraints_mentions_tool_and_prerequisite():
    text = render_constraints(WORKFLOW)
    assert "apply_refund" in text
    assert "query_order" in text
    assert "硬约束" in text


def test_render_constraints_empty_workflow_returns_empty():
    assert render_constraints({}) == ""


# ---------- referenced_workflow_tools ----------

def test_referenced_workflow_tools_collects_both_sides():
    assert referenced_workflow_tools(WORKFLOW) == {"apply_refund", "query_order"}


def test_referenced_workflow_tools_empty_on_bad_shape():
    assert referenced_workflow_tools({"guards": "坏"}) == set()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_workflow.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent.skills.workflow'`

- [ ] **Step 3: 实现 workflow.py**

创建 `app/agent/skills/workflow.py`：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_workflow.py -v`
Expected: PASS（24 passed）

- [ ] **Step 5: 提交**

```bash
git add app/agent/skills/workflow.py tests/test_skill_workflow.py
git commit -m "feat(skill): 工作流守卫判定(前置工具+参数校验,声明出错时 fail-open)"
```

---

### Task 2: 执行轨迹记录参数与拦截标记

**Files:**
- Modify: `app/agent/skills/execution_trace.py`
- Modify: `app/agent/chat.py`（`_execute_tool_call` 中的 `note_tool_call` 调用传 args）
- Test: `tests/test_skill_execution_trace.py`（追加用例）

**Interfaces:**
- Consumes: 无
- Produces（`SkillTurn` 签名变更）:
  - `SkillTurn.note_tool_call(name: str, result_str: str, args: dict | None = None) -> None`
    `tool_calls` 元素形状变为 `{"name", "ok", "error", "args"}`
  - `SkillTurn.note_blocked(name: str, args: dict | None, reason: str) -> None`
    追加 `{"name", "ok": False, "error": reason, "args", "blocked": True}`
  - `SkillTurn.outcome(requires_human: bool) -> str` —— **忽略 blocked 条目**，不因守卫拦截判 `tool_error`
  - `SkillTurn.blocked_count -> int`

> **为什么 blocked 不算失败**：守卫拦住一次跳步、模型随后补齐前置步骤并成功——这是守卫**起作用**，不是这轮失败。若计入 `tool_error`，守卫越有效灰度成功率越难看，会污染自进化闭环（另一份计划 Task 14）的 A/B 判定。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_skill_execution_trace.py` 末尾追加：

```python
# ---------- G1:工具参数与守卫拦截标记 ----------

def test_note_tool_call_records_args():
    turn = SkillTurn()
    turn.note_tool_call("query_order", json.dumps({"success": True}, ensure_ascii=False),
                        args={"order_id": "ORD-20240115-001"})
    assert turn.tool_calls[0]["args"] == {"order_id": "ORD-20240115-001"}


def test_note_tool_call_args_default_empty_dict():
    turn = SkillTurn()
    turn.note_tool_call("query_order", json.dumps({"success": True}, ensure_ascii=False))
    assert turn.tool_calls[0]["args"] == {}


def test_note_blocked_marks_entry():
    turn = SkillTurn()
    turn.note_blocked("apply_refund", {"order_id": "X"}, "退款前必须先查单")

    entry = turn.tool_calls[0]
    assert entry["name"] == "apply_refund"
    assert entry["ok"] is False
    assert entry["blocked"] is True
    assert "先查单" in entry["error"]
    assert turn.blocked_count == 1


def test_blocked_does_not_count_as_tool_error():
    """守卫拦截后模型补齐并成功 → 本轮算成功,不能因拦截判失败。"""
    turn = SkillTurn()
    turn.note_blocked("apply_refund", {}, "退款前必须先查单")
    turn.note_tool_call("query_order", json.dumps({"success": True}, ensure_ascii=False))
    turn.note_tool_call("apply_refund", json.dumps({"success": True}, ensure_ascii=False))

    assert turn.outcome(requires_human=False) == OUTCOME_SUCCESS
    assert turn.blocked_count == 1


def test_real_tool_failure_still_counts():
    turn = SkillTurn()
    turn.note_blocked("apply_refund", {}, "先查单")
    turn.note_tool_call("query_order", json.dumps({"success": False, "error": "订单不存在"},
                                                  ensure_ascii=False))
    assert turn.outcome(requires_human=False) == OUTCOME_TOOL_ERROR
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_execution_trace.py -v`
Expected: FAIL — `TypeError: note_tool_call() got an unexpected keyword argument 'args'`

- [ ] **Step 3: 扩展 SkillTurn**

在 `app/agent/skills/execution_trace.py` 中，把 `SkillTurn` 的 `note_tool_call` 与 `outcome` 替换为：

```python
    def note_tool_call(self, name: str, result_str: str, args: dict | None = None) -> None:
        """记录一次工具调用。若是成功的 load_skill,同时记下 skill 名与加载的版本。

        args 一并记录:G1 守卫要据此判定"前置调用的参数与本次是否一致"。
        """
        ok, error = _parse_result(result_str)
        if name == LOAD_SKILL_TOOL and ok:
            loaded = _loaded_skill_name(result_str)
            if loaded:
                self.skill_name = loaded
                self.variant = _loaded_variant(result_str)
        self.tool_calls.append({"name": name, "ok": ok, "error": error,
                                "args": dict(args or {})})

    def note_blocked(self, name: str, args: dict | None, reason: str) -> None:
        """记录一次被工作流守卫拦下的调用(未执行)。

        blocked 条目不计入 tool_error:守卫拦住跳步、模型随后补齐并成功,是守卫
        起作用而非本轮失败。若计入失败,守卫越有效灰度成功率越难看。
        """
        self.tool_calls.append({"name": name, "ok": False, "error": reason,
                                "args": dict(args or {}), "blocked": True})

    @property
    def blocked_count(self) -> int:
        return sum(1 for call in self.tool_calls if call.get("blocked"))

    def outcome(self, requires_human: bool) -> str:
        """本轮结局:转人工 > 真实工具失败 > 成功(守卫拦截不算失败)。"""
        if requires_human:
            return OUTCOME_HANDOFF
        if any(not call["ok"] and not call.get("blocked") for call in self.tool_calls):
            return OUTCOME_TOOL_ERROR
        return OUTCOME_SUCCESS
```

> 若 `variant` / `_loaded_variant` 尚未存在（另一份计划的 Task 13 未做），把 `self.variant = _loaded_variant(result_str)` 这一行删掉即可，其余不变。

- [ ] **Step 4: chat.py 传 args**

在 `app/agent/chat.py` 的 `_execute_tool_call` 中，把：

```python
                turn.note_tool_call(name, result_str)
```

改为：

```python
                turn.note_tool_call(name, result_str, args=args)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_execution_trace.py -v`
Expected: PASS（全部通过，含新增 5 条）

- [ ] **Step 6: 提交**

```bash
git add app/agent/skills/execution_trace.py app/agent/chat.py tests/test_skill_execution_trace.py
git commit -m "feat(skill): 执行轨迹记录工具参数与守卫拦截标记(拦截不计入失败率)"
```

---

### Task 3: 加载器暴露工作流声明并把硬约束告知模型

**Files:**
- Modify: `app/agent/skills/loader.py`（`SkillMeta` 加字段、`_discover` 解析、`load_skill` 渲染约束）
- Test: `tests/test_skill_workflow_loader.py`

**Interfaces:**
- Consumes: `parse_workflow` / `render_constraints`（Task 1）
- Produces:
  - `SkillMeta.workflow: dict`（默认 `{}`）
  - `SkillManager.get_workflow(skill_name: str) -> dict`
  - `SkillManager.load_skill(skill_name)` 返回的 `instructions` 末尾附加硬约束文案（有 guards 时）

> 让模型**事先**知道约束，而不是靠被拒绝才发现——省一轮往返，也让日志更干净。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_workflow_loader.py`：

```python
"""G1 加载器接线:解析 workflow 声明、把硬约束附进 skill 指令。"""

from app.agent.skills.loader import SkillManager

WITH_WORKFLOW = """---
name: process-return
description: 退货退款处理。
workflow:
  slots:
    order_id:
      pattern: '^ORD-\\d{8}-[\\w-]+$'
      hint: '订单号形如 ORD-20240115-001'
  guards:
    - tool: apply_refund
      requires_tools: [query_order]
      same_args: [order_id]
      validate: [order_id, reason]
      deny: '退款前必须先用 query_order 核对该订单'
---

## 退货流程
第一步：调用 `query_order` 核对订单。
"""

WITHOUT_WORKFLOW = """---
name: track-order
description: 订单物流跟踪。
---

## 查物流
调用 `query_logistics`。
"""

BAD_WORKFLOW = """---
name: broken-skill
description: 声明写坏了。
workflow: 这不是字典
---
正文。
"""


def _mgr(tmp_path, files: dict):
    definitions = tmp_path / "definitions"
    for name, content in files.items():
        (definitions / name).mkdir(parents=True)
        (definitions / name / "SKILL.md").write_text(content, encoding="utf-8")
    return SkillManager(skills_dir=str(definitions), enabled=True)


def test_get_workflow_returns_parsed_block(tmp_path):
    mgr = _mgr(tmp_path, {"process-return": WITH_WORKFLOW})
    wf = mgr.get_workflow("process-return")

    assert wf["guards"][0]["tool"] == "apply_refund"
    assert wf["slots"]["order_id"]["pattern"].startswith("^ORD-")


def test_get_workflow_empty_without_declaration(tmp_path):
    mgr = _mgr(tmp_path, {"track-order": WITHOUT_WORKFLOW})
    assert mgr.get_workflow("track-order") == {}


def test_get_workflow_empty_for_unknown_skill(tmp_path):
    mgr = _mgr(tmp_path, {"track-order": WITHOUT_WORKFLOW})
    assert mgr.get_workflow("nope") == {}


def test_bad_workflow_block_does_not_break_discovery(tmp_path):
    """声明写坏的 skill 仍能被加载(只是没有约束),不能拖垮整个目录扫描。"""
    mgr = _mgr(tmp_path, {"broken-skill": BAD_WORKFLOW, "track-order": WITHOUT_WORKFLOW})
    assert set(mgr.skill_names) == {"broken-skill", "track-order"}
    assert mgr.get_workflow("broken-skill") == {}


def test_load_skill_appends_hard_constraints(tmp_path):
    mgr = _mgr(tmp_path, {"process-return": WITH_WORKFLOW})
    result = mgr.load_skill("process-return")

    assert result["success"] is True
    assert "第一步" in result["instructions"]        # 原 body 保留
    assert "硬约束" in result["instructions"]         # 约束已附加
    assert "apply_refund" in result["instructions"]
    assert "query_order" in result["instructions"]


def test_load_skill_without_workflow_unchanged(tmp_path):
    mgr = _mgr(tmp_path, {"track-order": WITHOUT_WORKFLOW})
    result = mgr.load_skill("track-order")

    assert "硬约束" not in result["instructions"]
    assert result["instructions"].strip().startswith("## 查物流")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_workflow_loader.py -v`
Expected: FAIL — `AttributeError: 'SkillManager' object has no attribute 'get_workflow'`

- [ ] **Step 3: SkillMeta 加字段**

在 `app/agent/skills/loader.py` 中，把 `SkillMeta` 的字段声明：

```python
    name: str
    description: str
    path: Path
    body: str = ""
    _body_loaded: bool = field(default=False, repr=False)
```

改为：

```python
    name: str
    description: str
    path: Path
    body: str = ""
    workflow: dict = field(default_factory=dict)   # G1 工作流声明(无则空,行为不变)
    _body_loaded: bool = field(default=False, repr=False)
```

- [ ] **Step 4: _discover 解析声明**

在 `app/agent/skills/loader.py` 的 `_discover` 中，把：

```python
            self._skills[name] = SkillMeta(
                name=name, description=description, path=skill_file,
            )
```

改为：

```python
            from app.agent.skills.workflow import parse_workflow

            self._skills[name] = SkillMeta(
                name=name, description=description, path=skill_file,
                workflow=parse_workflow(meta),
            )
```

- [ ] **Step 5: 加 get_workflow 并在 load_skill 附加约束**

在 `app/agent/skills/loader.py` 的 `load_skill` 方法之前插入：

```python
    def get_workflow(self, skill_name: str) -> dict:
        """该 skill 的工作流声明(无声明/未知 skill → {},即无约束)。"""
        skill = self._skills.get(skill_name)
        return dict(skill.workflow) if skill else {}
```

在 `load_skill` 中，把：

```python
        return {
            "success": True,
            "skill_name": skill.name,
            "instructions": body,
            "variant": variant,
        }
```

改为：

```python
        # G1:把硬约束附在指令后,让模型事先知道(而不是被拦回才发现),省一轮往返
        from app.agent.skills.workflow import render_constraints

        return {
            "success": True,
            "skill_name": skill.name,
            "instructions": body + render_constraints(skill.workflow),
            "variant": variant,
        }
```

> 若另一份计划的 Task 13（灰度）尚未实施，此处 `load_skill` 没有 `variant` 变量与键——把 `"variant": variant,` 一行删掉，只加 `render_constraints` 部分。

- [ ] **Step 6: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_workflow_loader.py -v`
Expected: PASS（6 passed）

- [ ] **Step 7: 回归既有 skill 测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skills.py tests/test_skill_frontmatter.py tests/test_skill_synth.py -v`
Expected: PASS。

若有用例断言 `instructions` **完全等于**某段 body 文本而失败：那是本任务的预期变化（无 `workflow:` 声明的 skill 不受影响；有声明的会多出约束段）。把断言改为 `assert body_text in result["instructions"]`。

- [ ] **Step 8: 提交**

```bash
git add app/agent/skills/loader.py tests/test_skill_workflow_loader.py
git commit -m "feat(skill): 加载器解析工作流声明并把硬约束告知模型"
```

---

### Task 4: 守卫接入 ReAct（拦截跳步调用）

**Files:**
- Modify: `app/agent/chat.py`（`_execute_tool_call` 入口）
- Test: `tests/test_skill_workflow_wiring.py`

**Interfaces:**
- Consumes: `evaluate_guards`（Task 1）、`SkillTurn.note_blocked` / `tool_calls`（Task 2）、`SkillManager.get_workflow`（Task 3）
- Produces:
  - `EcomAgent._workflow_denial(name: str, args: dict) -> str | None`（内部方法）
  - 拦截时 `_execute_tool_call` 返回 `{"success": false, "error": <理由>, "workflow_guard": true}` 的 JSON 串，并写入 `raw_messages` 的 tool 消息——**不执行真实工具**

> **为什么把拒绝走工具结果通道**：ReAct 循环本来就会把工具结果喂回模型，模型看到"必须先调 query_order"会自己补上，下一步再调 `apply_refund` 就放行了。这样零改动 ReAct 结构，且对话不中断。
>
> **fail-open**：守卫判定过程中任何异常都放行（记 warning 事件），绝不能让守卫本身的 bug 导致工具全线不可用。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_skill_workflow_wiring.py`：

```python
"""G1 接线:守卫在 ReAct 里真的拦住跳步调用,且拒绝走工具结果通道回传模型。"""

import json

from app.agent.chat import EcomAgent
from app.agent.skills.execution_trace import SkillTurn

WORKFLOW = {
    "slots": {"order_id": {"pattern": r"^ORD-\d{8}-[\w-]+$",
                           "hint": "订单号形如 ORD-20240115-001"},
              "reason": {"min_length": 2}},
    "guards": [{"tool": "apply_refund", "requires_tools": ["query_order"],
                "same_args": ["order_id"], "validate": ["order_id", "reason"],
                "deny": "退款前必须先用 query_order 核对该订单"}],
}


class _FakeSkillManager:
    def __init__(self, workflow):
        self._workflow = workflow
        self.enabled = True

    def get_workflow(self, skill_name):
        return self._workflow


class _FakeToolManager:
    """记录真实工具是否被执行过。"""

    def __init__(self):
        self.executed = []

    def execute_tool(self, name, arguments):
        self.executed.append((name, dict(arguments)))
        return json.dumps({"success": True, "ok": name}, ensure_ascii=False)


def _agent(workflow, skill_name="process-return"):
    """构造一个只装了守卫所需部件的 agent(不跑 __init__,不触网)。"""
    agent = EcomAgent.__new__(EcomAgent)
    agent.session_id = "s-guard"
    agent.user_id = "u-guard"
    agent.skill_manager = _FakeSkillManager(workflow)
    agent.tool_manager = _FakeToolManager()
    agent.raw_messages = []
    agent._pending = None
    agent._step_seq = 0
    agent.event_sink = None
    agent._skill_turn = SkillTurn()
    agent._skill_turn.skill_name = skill_name
    agent._checkpoint = lambda *a, **k: None
    agent._emit = lambda *a, **k: None
    return agent


# ---------- _workflow_denial ----------

def test_denial_when_prerequisite_missing():
    agent = _agent(WORKFLOW)
    msg = agent._workflow_denial("apply_refund",
                                 {"order_id": "ORD-20240115-001", "reason": "尺码不合适"})
    assert msg is not None
    assert "query_order" in msg


def test_no_denial_after_prerequisite_satisfied():
    agent = _agent(WORKFLOW)
    agent._skill_turn.note_tool_call(
        "query_order", json.dumps({"success": True}, ensure_ascii=False),
        args={"order_id": "ORD-20240115-001"})

    assert agent._workflow_denial(
        "apply_refund", {"order_id": "ORD-20240115-001", "reason": "尺码不合适"}) is None


def test_no_denial_for_unguarded_tool():
    agent = _agent(WORKFLOW)
    assert agent._workflow_denial("query_product", {"keyword": "鞋"}) is None


def test_no_denial_when_no_skill_loaded():
    """本轮没加载 skill → 无 skill 级约束(由 consent 门单独守授权)。"""
    agent = _agent(WORKFLOW, skill_name="")
    assert agent._workflow_denial("apply_refund", {}) is None


def test_denial_fails_open_on_broken_manager():
    """守卫自身出错必须放行,不能让工具全线不可用。"""
    class Boom:
        enabled = True

        def get_workflow(self, name):
            raise RuntimeError("boom")

    agent = _agent(WORKFLOW)
    agent.skill_manager = Boom()
    assert agent._workflow_denial("apply_refund", {}) is None


# ---------- _execute_tool_call 拦截 ----------

def test_blocked_call_does_not_execute_real_tool():
    agent = _agent(WORKFLOW)
    result = agent._execute_tool_call(
        "tc-1", "apply_refund", {"order_id": "ORD-20240115-001", "reason": "尺码不合适"})

    data = json.loads(result)
    assert data["success"] is False
    assert data["workflow_guard"] is True
    assert "query_order" in data["error"]
    assert agent.tool_manager.executed == []          # 真实工具没被调用


def test_blocked_call_written_into_history_for_model_to_react():
    agent = _agent(WORKFLOW)
    agent._execute_tool_call("tc-1", "apply_refund",
                             {"order_id": "ORD-20240115-001", "reason": "尺码不合适"})

    tool_msgs = [m for m in agent.raw_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "tc-1"
    assert "query_order" in tool_msgs[0]["content"]   # 模型能看到该补什么


def test_blocked_call_recorded_as_blocked_not_failure():
    agent = _agent(WORKFLOW)
    agent._execute_tool_call("tc-1", "apply_refund",
                             {"order_id": "ORD-20240115-001", "reason": "尺码不合适"})

    assert agent._skill_turn.blocked_count == 1
    assert agent._skill_turn.outcome(requires_human=False) == "success"


def test_allowed_call_executes_and_records_args():
    agent = _agent(WORKFLOW)
    agent._execute_tool_call("tc-1", "query_order", {"order_id": "ORD-20240115-001"})

    assert agent.tool_manager.executed == [("query_order", {"order_id": "ORD-20240115-001"})]
    assert agent._skill_turn.tool_calls[0]["args"] == {"order_id": "ORD-20240115-001"}


def test_full_sequence_query_then_refund_succeeds():
    """端到端顺序:先查单 → 再退款,第二步应放行并真正执行。"""
    agent = _agent(WORKFLOW)
    agent._execute_tool_call("tc-1", "query_order", {"order_id": "ORD-20240115-001"})
    agent._execute_tool_call("tc-2", "apply_refund",
                             {"order_id": "ORD-20240115-001", "reason": "尺码不合适"})

    executed = [name for name, _ in agent.tool_manager.executed]
    assert executed == ["query_order", "apply_refund"]
    assert agent._skill_turn.blocked_count == 0


def test_refund_on_different_order_than_queried_is_blocked():
    """查的是 A 单、退的是 B 单 —— 最危险的跳步,必须拦住。"""
    agent = _agent(WORKFLOW)
    agent._execute_tool_call("tc-1", "query_order", {"order_id": "ORD-20240115-001"})
    result = agent._execute_tool_call("tc-2", "apply_refund",
                                      {"order_id": "ORD-20991231-999", "reason": "不要了"})

    assert json.loads(result)["workflow_guard"] is True
    assert [n for n, _ in agent.tool_manager.executed] == ["query_order"]   # 退款没执行


def test_bad_param_format_is_blocked():
    agent = _agent(WORKFLOW)
    agent._execute_tool_call("tc-1", "query_order", {"order_id": "12345"})
    result = agent._execute_tool_call("tc-2", "apply_refund",
                                      {"order_id": "12345", "reason": "不要了"})

    data = json.loads(result)
    assert data["workflow_guard"] is True
    assert "order_id" in data["error"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_workflow_wiring.py -v`
Expected: FAIL — `AttributeError: 'EcomAgent' object has no attribute '_workflow_denial'`

- [ ] **Step 3: 实现 `_workflow_denial`**

在 `app/agent/chat.py` 中，`_execute_tool_call` 方法**之前**插入：

```python
    def _workflow_denial(self, name: str, args: dict) -> str | None:
        """G1:本轮已加载的 skill 是否禁止此刻调用该工具(前置步骤/参数未满足)。

        返回 None = 放行;字符串 = 拒绝理由(会被当作工具结果回传给模型)。
        fail-open:守卫判定自身任何异常都放行——绝不能让守卫的 bug 让工具全线不可用。
        """
        turn = getattr(self, "_skill_turn", None)
        if turn is None or not getattr(turn, "skill_name", ""):
            return None            # 本轮没加载 skill → 无 skill 级约束
        manager = getattr(self, "skill_manager", None)
        if manager is None or not getattr(manager, "enabled", False):
            return None
        try:
            from app.agent.skills.workflow import evaluate_guards

            workflow = manager.get_workflow(turn.skill_name)
            if not workflow:
                return None
            return evaluate_guards(workflow, name, args, turn.tool_calls)
        except Exception:  # noqa: BLE001 守卫出错=放行,不阻断业务
            return None
```

- [ ] **Step 4: 在 `_execute_tool_call` 入口拦截**

在 `app/agent/chat.py` 的 `_execute_tool_call` 中，把开头这一段：

```python
        self._emit({"type": "tool_call", "name": name, "args": args})          # before
        result_str = self.tool_manager.execute_tool(name, args)
```

改为：

```python
        self._emit({"type": "tool_call", "name": name, "args": args})          # before

        # G1 工作流守卫:前置步骤/参数未满足 → 不执行工具,把拒绝当作工具结果回传,
        # 模型据此自行补齐前置步骤(零改动 ReAct 结构,对话不中断)。
        denial = self._workflow_denial(name, args)
        if denial is not None:
            result_str = json.dumps(
                {"success": False, "error": denial, "workflow_guard": True},
                ensure_ascii=False)
            self._emit({"type": "workflow_guard", "name": name, "reason": denial})
            turn = getattr(self, "_skill_turn", None)
            if turn is not None:
                try:
                    turn.note_blocked(name, args, denial)
                except Exception:  # noqa: BLE001
                    pass
            self.raw_messages.append(
                {"role": "tool", "tool_call_id": tool_call_id, "content": result_str})
            self._step_seq += 1
            self._checkpoint("in_flight")
            return result_str

        result_str = self.tool_manager.execute_tool(name, args)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_workflow_wiring.py -v`
Expected: PASS（12 passed）

- [ ] **Step 6: 回归 agent / 确认流测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent.py tests/test_consent.py tests/test_consent_gate_tools.py tests/test_checkpoint.py tests/test_skills.py -v`
Expected: PASS。既有 skill 都没有 `workflow:` 声明 ⇒ `get_workflow` 返回 `{}` ⇒ 守卫恒放行，行为不变。

- [ ] **Step 7: 提交**

```bash
git add app/agent/chat.py tests/test_skill_workflow_wiring.py
git commit -m "feat(skill): 工作流守卫接入 ReAct(拦截跳步调用,拒绝走工具结果通道)"
```

---

### Task 5: 候选校验支持工作流声明

**Files:**
- Modify: `app/agent/skills/validator.py`
- Test: `tests/test_skill_validator.py`（追加用例）

**Interfaces:**
- Consumes: `referenced_workflow_tools`（Task 1）、`known_tool_names`（既有）
- Produces: `validate_candidate(content, known=None)` 现在也校验 `workflow:` 声明里引用的工具名；`unknown_tools` 合并两处来源

> 自进化合成出的候选如果带 `workflow:` 声明，声明里的工具名同样可能是编造的（实测发生过 `order_list`）。声明里的错工具比正文里的更危险——正文错模型可能自己绕开，声明错会直接让守卫拦死真实调用。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_skill_validator.py` 末尾追加：

```python
# ---------- G1:workflow 声明里的工具名同样要校验 ----------

WORKFLOW_GOOD = """---
name: process-return
description: 退货处理。
workflow:
  guards:
    - tool: apply_refund
      requires_tools: [query_order]
      deny: '先查单'
---
第一步：调用 `query_order`。
"""

WORKFLOW_BAD_TOOL = """---
name: process-return
description: 退货处理。
workflow:
  guards:
    - tool: refund_apply
      requires_tools: [order_lookup]
      deny: '先查单'
---
正文不引用任何工具。
"""


def test_validate_accepts_workflow_with_real_tools():
    result = validate_candidate(WORKFLOW_GOOD)
    assert result["valid"] is True
    assert result["unknown_tools"] == []


def test_validate_flags_unknown_tools_in_workflow_block():
    """声明里的错工具比正文里的更危险:会让守卫拦死真实调用。"""
    result = validate_candidate(WORKFLOW_BAD_TOOL)
    assert result["valid"] is False
    assert result["unknown_tools"] == ["order_lookup", "refund_apply"]


def test_validate_merges_body_and_workflow_unknown_tools():
    content = WORKFLOW_BAD_TOOL.replace("正文不引用任何工具。", "还要调用 `bogus_tool`。")
    result = validate_candidate(content)
    assert set(result["unknown_tools"]) == {"bogus_tool", "order_lookup", "refund_apply"}


def test_validate_ignores_malformed_workflow_block():
    content = """---
name: x
description: d
workflow: 这不是字典
---
调用 `query_order`。
"""
    result = validate_candidate(content)
    assert result["valid"] is True     # 坏声明不额外报错(加载时同样按无约束处理)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_validator.py -v`
Expected: FAIL — `test_validate_flags_unknown_tools_in_workflow_block` 断言失败（`unknown_tools` 为 `[]`，声明里的工具名还没被检查）

- [ ] **Step 3: 扩展 validate_candidate**

在 `app/agent/skills/validator.py` 中，把 `validate_candidate` 里的这一段：

```python
    known_set = known if known is not None else known_tool_names()
    unknown = sorted(referenced_tools(content) - known_set)
```

改为：

```python
    from app.agent.skills.workflow import parse_workflow, referenced_workflow_tools

    known_set = known if known is not None else known_tool_names()
    # 正文引用 + workflow 声明引用一起校验:声明里的错工具会让守卫拦死真实调用,更危险
    referenced = referenced_tools(content) | referenced_workflow_tools(parse_workflow(meta))
    unknown = sorted(referenced - known_set)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_validator.py -v`
Expected: PASS（12 passed）

- [ ] **Step 5: 回归合成校验测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skill_synth_validation.py tests/test_promote_skill.py -v`
Expected: PASS（若另一份计划的对应任务已实施）。

- [ ] **Step 6: 提交**

```bash
git add app/agent/skills/validator.py tests/test_skill_validator.py
git commit -m "feat(skill): 候选校验覆盖 workflow 声明里的工具名"
```

---

### Task 6: 给 process-return 写真实工作流声明并端到端验证

**Files:**
- Modify: `app/agent/skills/definitions/process-return/SKILL.md`（加 `workflow:` 声明）
- Test: `tests/test_process_return_workflow.py`

**Interfaces:**
- Consumes: 全部前 5 个任务
- Produces: 正式库首个带守卫的 skill

> `process-return/SKILL.md` 第 52 行原文写着「退款是敏感操作，申请前必须与用户确认订单号和退款原因，**不能跳过**」——本任务让这句话第一次真正生效。
>
> 只给这一个 skill 加。`track-order` / `product-recommend` 是只读咨询类，保持纯 markdown。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_process_return_workflow.py`：

```python
"""正式库 process-return 的工作流声明:让"退款前必须确认订单号与原因"真正生效。"""

from app.agent.skills.loader import SkillManager
from app.agent.skills.validator import validate_candidate
from app.agent.skills.workflow import evaluate_guards

SKILL_PATH = "app/agent/skills/definitions/process-return/SKILL.md"


def _workflow():
    mgr = SkillManager(skills_dir="app/agent/skills/definitions", enabled=True)
    return mgr.get_workflow("process-return")


def test_process_return_declares_refund_guard():
    wf = _workflow()
    tools = [g["tool"] for g in wf.get("guards", [])]
    assert "apply_refund" in tools


def test_refund_blocked_without_prior_query_order():
    wf = _workflow()
    msg = evaluate_guards(wf, "apply_refund",
                          {"order_id": "ORD-20240115-001", "reason": "尺码不合适"}, [])
    assert msg is not None
    assert "query_order" in msg


def test_refund_allowed_after_query_order():
    wf = _workflow()
    prior = [{"name": "query_order", "ok": True, "args": {"order_id": "ORD-20240115-001"}}]
    assert evaluate_guards(wf, "apply_refund",
                           {"order_id": "ORD-20240115-001", "reason": "尺码不合适"},
                           prior) is None


def test_refund_blocked_on_empty_reason():
    """"必须与用户确认退款原因"——空原因不放行。"""
    wf = _workflow()
    prior = [{"name": "query_order", "ok": True, "args": {"order_id": "ORD-20240115-001"}}]
    msg = evaluate_guards(wf, "apply_refund",
                          {"order_id": "ORD-20240115-001", "reason": ""}, prior)
    assert msg is not None
    assert "reason" in msg


def test_refund_blocked_on_fabricated_order_id_format():
    wf = _workflow()
    prior = [{"name": "query_order", "ok": True, "args": {"order_id": "P-987654321"}}]
    msg = evaluate_guards(wf, "apply_refund",
                          {"order_id": "P-987654321", "reason": "不要了"}, prior)
    assert msg is not None
    assert "order_id" in msg


def test_skill_file_passes_validator():
    """正式库这份声明本身必须过校验(工具名真实、frontmatter 完整)。"""
    from pathlib import Path

    report = validate_candidate(Path(SKILL_PATH).read_text(encoding="utf-8"))
    assert report["valid"] is True, report["errors"]


def test_readonly_skills_have_no_workflow():
    """只读咨询类保持纯 markdown,不加约束(灵活性优先)。"""
    mgr = SkillManager(skills_dir="app/agent/skills/definitions", enabled=True)
    assert mgr.get_workflow("track-order") == {}
    assert mgr.get_workflow("product-recommend") == {}


def test_loaded_instructions_carry_hard_constraints():
    mgr = SkillManager(skills_dir="app/agent/skills/definitions", enabled=True)
    result = mgr.load_skill("process-return")
    assert "硬约束" in result["instructions"]
    assert "退货退款处理流程" in result["instructions"]   # 原 body 未丢
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_process_return_workflow.py -v`
Expected: FAIL — `assert "apply_refund" in []`（还没有声明）

- [ ] **Step 3: 给 SKILL.md 加声明**

在 `app/agent/skills/definitions/process-return/SKILL.md` 中，把开头的 frontmatter：

```
---
name: process-return
description: 当用户要退货、退款、换货时使用。指导客服完成完整的退货退款流程：确认订单信息、验证退货资格、检索退换货政策、申请退款、告知后续进度。适用关键词：退货、退款、换货、不想要了、质量问题、尺码不合适。
---
```

替换为：

```
---
name: process-return
description: 当用户要退货、退款、换货时使用。指导客服完成完整的退货退款流程：确认订单信息、验证退货资格、检索退换货政策、申请退款、告知后续进度。适用关键词：退货、退款、换货、不想要了、质量问题、尺码不合适。
workflow:
  slots:
    order_id:
      pattern: '^ORD-\d{8}-[\w-]+$'
      hint: '订单号形如 ORD-20240115-001；查不到时请先用 list_user_orders 让用户确认'
    reason:
      min_length: 2
      hint: '退款原因必须先与用户确认，不能替用户填写'
  guards:
    - tool: apply_refund
      requires_tools: [query_order]
      same_args: [order_id]
      validate: [order_id, reason]
      deny: '退款前必须先用 query_order 核对该订单，并与用户确认退款原因（本流程第一步与第四步）'
---
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_process_return_workflow.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: PASS（无新增失败）

- [ ] **Step 6: 提交**

```bash
git add app/agent/skills/definitions/process-return/SKILL.md tests/test_process_return_workflow.py
git commit -m "feat(skill): process-return 加工作流声明(退款前强制核对订单与原因)"
```

---

## 端到端验收（全部任务完成后人工跑一遍）

- [ ] **1. 守卫真的拦得住（核心验收）**：启动服务，在聊天里**直接**说「订单 ORD-20240115-001 我要退款，原因不合适，直接退」——诱导模型跳过查单直接退。

预期：模型第一次调 `apply_refund` 被拦（前端"Agent 思考过程"里能看到 `workflow_guard` 事件），随后它会先调 `query_order` 再退款成功。最终回复正常，用户看不到中间的拦截。

- [ ] **2. 拦截被正确记账（不污染成功率）**：

```bash
.venv/Scripts/python.exe -c "
from app.db import get_db
r = get_db().list_skill_traces(skill_name='process-return', limit=3)
for t in r:
    print(t['outcome'], [(c['name'], c.get('blocked', False)) for c in t['tool_calls']])
"
```
预期：出现 `('apply_refund', True)` 的 blocked 条目，但该行 `outcome` 仍是 `success`（守卫拦截不算失败）。

- [ ] **3. 无声明的 skill 完全不受影响**：问「我的订单到哪了」（走 `track-order`）。
预期：正常查物流，无任何 `workflow_guard` 事件。

- [ ] **4. 模型事先知道约束（省往返）**：查看 `load_skill` 返回内容里是否带硬约束段：

```bash
.venv/Scripts/python.exe -c "
from app.agent.skills.loader import SkillManager
m = SkillManager(skills_dir='app/agent/skills/definitions', enabled=True)
print(m.load_skill('process-return')['instructions'][-400:])
"
```
预期：末尾出现「## 本技能的硬约束(系统强制,违反会被拦回)」段落。

- [ ] **5. 坏声明不会搞挂系统（fail-open 回归，必须验）**：临时把 `process-return/SKILL.md` 的 `workflow:` 改成 `workflow: 乱写`，重启后问退款。
预期：**服务正常、退款流程照常工作**（只是没有守卫）。验完记得 `git checkout` 还原。

---

## 已知边界（诚实记录，不要在对外描述里回避）

1. **守卫是 skill 作用域的**：若模型**没有加载** `process-return` 就直接调 `apply_refund`，本守卫不生效。缓解有两层：① catalog prompt 已强制要求匹配场景时首轮加载 skill；② `consent.py` 的授权门对 `apply_refund` 无条件生效（用户没批就不执行）。**但"必须先查单"这条顺序约束在未加载 skill 时确实不覆盖**。若要全局覆盖，需要一份与 skill 无关的全局守卫表——本计划刻意没做（YAGNI，且会与 consent 职责重叠）。
2. **只做前置守卫，不做步骤编排与收口**：G1 原始设想的四段「前置校验 → 工具序列 → 分支 → 收口」，本计划**强制了前两段**（前置校验、必经工具顺序），**分支与收口仍由模型按 markdown 执行**。多客提到的「分支判断」（订单 pending/shipped/delivered 走不同路径）目前靠模型读 `query_order` 的 status 自行判断——声明式分支需要读**前置工具的返回内容**（不只是参数），是更重的机制，见下方草图。
3. **不改模型、不改 ReAct 结构**：守卫只在工具执行入口拦一刀。这是刻意的——保持可回滚、可关闭。

## 后续可选：声明式 Runner（草图，不建议现在做）

若将来确实需要"完全确定性的流程执行"（例如接入需要审计留痕的支付/理赔流程），设计方向是：

- `workflow.steps` 声明有序步骤 + `when` 条件分支（条件可引用前置工具返回值的 JSON 路径，如 `${query_order.status} == 'delivered'`）
- 新增 `app/agent/skills/runner.py`：命中该 skill 时**绕过 ReAct**，由 runner 按声明顺序执行工具、按分支跳转、缺参数时向用户提问（槽位填充），最后用 LLM 只做一件事——把结构化结果说成人话
- 收益：完全确定、可审计、每步不烧 LLM
- 成本：新增一整条执行路径（与 ReAct 并行维护）；流程一旦遇到未预设分支会卡死，需要兜底回落 ReAct

**为什么不现在做**：Task 1–6 的守卫已经拿到「顺序与参数确定性」这个核心收益，成本只有一个纯函数模块 + 一处拦截。Runner 的增量收益（步骤编排也确定）远小于其复杂度与维护成本。等到真有一条流程被反复证明"模型编排不可靠"，再针对那一条做 runner。

**注意**：`app/agent/strategies/`（Plan-and-Execute / Reflexion / REWOO，目前只有空 docstring）**不是**这件事的落地位置。Plan-and-Execute 是"LLM 自己写计划再执行"，依然是概率性的；工作流要的是"作者声明、确定性执行"。两者是不同特性，不要混做。

## 完成后可以准确对外描述的能力

| 多客说法 | 本计划落地物 |
|---|---|
| 基础单元为独立工作流 Skill | 高危 skill 带声明式 `workflow` 块（slots + guards），只读咨询类保持指令式 |
| 补充参数校验步骤 | `slots` 的 pattern / min_length / 非空校验，未过不放行 |
| 补充分支判断 | **部分**：前置条件判断已声明式强制；状态分支仍由模型按 markdown 判断（边界已记录） |

**可以如实说**：「高风险流程（退款/取消/改址）采用声明式工作流守卫：前置步骤未完成、参数未校验通过时，工具调用被系统拦回，模型须补齐后重试——流程合规性由代码强制，不依赖模型自觉。低风险咨询类保持指令式以保留灵活性（对齐 Agent Skills 开放标准）。」

**不能说**：「全流程状态机驱动」/「完全确定性执行」——步骤编排仍在 ReAct 里，只有前置条件是硬的。
