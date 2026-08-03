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


def test_prerequisites_distinguish_falsy_value_from_missing():
    """same_args 比较不能把 0/False 与"字段缺失"混为一谈,否则守卫对数值字段形同虚设。"""
    guard = {"tool": "apply_refund", "requires_tools": ["query_order"], "same_args": ["amount"]}
    prior = [{"name": "query_order", "ok": True, "args": {}}]   # amount 字段缺失
    assert check_prerequisites(guard, {"amount": 0}, prior) is not None


def test_prerequisites_match_on_equal_numeric_values():
    """数值相等仍应判一致(归一化后比较,不是要求类型相同)。"""
    guard = {"tool": "apply_refund", "requires_tools": ["query_order"], "same_args": ["amount"]}
    prior = [{"name": "query_order", "ok": True, "args": {"amount": 100}}]
    assert check_prerequisites(guard, {"amount": "100"}, prior) is None


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
