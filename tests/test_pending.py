"""挂起动作判定(R3):evaluate_pending 据工具返回决定 set/clear/keep;PendingAction 可序列化。"""

import json

from app.agent.pending import PendingAction, evaluate_pending


def test_pending_action_roundtrip():
    p = PendingAction("refund", "apply_refund", {"order_id": "O1", "reason": "x"}, "确认?")
    d = p.to_dict()
    assert d == {"action": "refund", "tool_name": "apply_refund",
                 "args": {"order_id": "O1", "reason": "x"}, "message": "确认?"}
    assert PendingAction.from_dict(d) == p


def test_need_confirm_sets_pending():
    result = json.dumps({"success": False, "need_confirm": True, "action": "refund",
                         "message": "退款是敏感操作,请确认?"})
    act, pa = evaluate_pending("apply_refund", {"order_id": "O1", "reason": "尺码"}, result)
    assert act == "set"
    assert pa.action == "refund" and pa.tool_name == "apply_refund"
    assert pa.args == {"order_id": "O1", "reason": "尺码"}


def test_risk_tool_success_clears():
    ok = json.dumps({"success": True, "message": "退款已提交"})
    assert evaluate_pending("apply_refund", {"order_id": "O1"}, ok) == ("clear", None)
    assert evaluate_pending("cancel_order", {"order_id": "O1"}, ok) == ("clear", None)


def test_non_risk_tool_keeps():
    ok = json.dumps({"success": True, "order": {}})
    assert evaluate_pending("query_order", {"order_id": "O1"}, ok) == ("keep", None)


def test_bad_or_nonrisk_result_keeps():
    assert evaluate_pending("apply_refund", {}, "not-json") == ("keep", None)
    assert evaluate_pending(
        "some_tool", {}, json.dumps({"need_confirm": True, "action": "other"})
    ) == ("keep", None)
