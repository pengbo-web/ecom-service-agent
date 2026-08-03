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
