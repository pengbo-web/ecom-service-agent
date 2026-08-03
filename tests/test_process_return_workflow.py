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
