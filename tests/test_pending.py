import json

from app.agent.pending import (
    PendingAction, PendingActionStore, observe_tool_result,
    get_pending_store, set_pending_store,
)


def _fresh():
    s = PendingActionStore()
    set_pending_store(s)
    return s


def test_remember_get_pop():
    s = _fresh()
    p = PendingAction("refund", "apply_refund", {"order_id": "O1", "reason": "x"}, "确认?")
    s.remember("sess", p)
    assert s.get("sess") is p
    assert s.pop("sess") is p
    assert s.get("sess") is None


def test_empty_session_id_ignored():
    s = _fresh()
    s.remember("", PendingAction("refund", "apply_refund", {}, ""))
    assert s.get("") is None


def test_observe_records_need_confirm():
    s = _fresh()
    result = json.dumps({"success": False, "need_confirm": True,
                         "action": "refund", "message": "确认退款?"})
    observe_tool_result("sess", "apply_refund", {"order_id": "O1", "reason": "尺码"}, result)
    p = s.get("sess")
    assert p is not None and p.action == "refund"
    assert p.args == {"order_id": "O1", "reason": "尺码"}


def test_observe_clears_on_success():
    s = _fresh()
    s.remember("sess", PendingAction("refund", "apply_refund", {"order_id": "O1"}, "?"))
    ok = json.dumps({"success": True, "message": "退款申请已提交"})
    observe_tool_result("sess", "apply_refund", {"order_id": "O1"}, ok)
    assert s.get("sess") is None


def test_observe_ignores_plain_tool_result():
    s = _fresh()
    observe_tool_result("sess", "query_order", {"order_id": "O1"},
                        json.dumps({"order": {"status": "shipped"}}))
    assert s.get("sess") is None


def test_observe_ignores_non_json():
    s = _fresh()
    observe_tool_result("sess", "apply_refund", {}, "not-json")
    assert s.get("sess") is None


def test_observe_ignores_unknown_action():
    s = _fresh()
    # need_confirm 但 action 不在 RISK_ACTIONS → 不记
    result = json.dumps({"need_confirm": True, "action": "unknown", "message": "?"})
    observe_tool_result("sess", "some_tool", {}, result)
    assert s.get("sess") is None
