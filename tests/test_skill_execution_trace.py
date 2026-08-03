"""G2 埋点纯逻辑:SkillTurn 从工具调用结果推断加载的 skill 与本轮结局。"""

import json

from app.agent.skills.execution_trace import (
    OUTCOME_HANDOFF,
    OUTCOME_SUCCESS,
    OUTCOME_TOOL_ERROR,
    SkillTurn,
)


def _load_skill_ok(name="process-return"):
    return json.dumps({"success": True, "skill_name": name, "instructions": "步骤..."},
                      ensure_ascii=False)


def test_fresh_turn_has_no_skill():
    turn = SkillTurn()
    assert turn.has_skill is False
    assert turn.tool_calls == []


def test_load_skill_success_records_skill_name():
    turn = SkillTurn()
    turn.note_tool_call("load_skill", _load_skill_ok())
    assert turn.skill_name == "process-return"
    assert turn.has_skill is True


def test_load_skill_failure_does_not_record_skill_name():
    turn = SkillTurn()
    turn.note_tool_call("load_skill", json.dumps({"success": False, "error": "未找到技能"},
                                                 ensure_ascii=False))
    assert turn.skill_name == ""
    assert turn.has_skill is False
    assert turn.tool_calls[0]["ok"] is False


def test_tool_call_ok_and_error_flags():
    turn = SkillTurn()
    turn.note_tool_call("query_order", json.dumps({"success": True, "order": {}}, ensure_ascii=False))
    turn.note_tool_call("apply_refund", json.dumps({"success": False, "error": "订单不存在"},
                                                   ensure_ascii=False))
    turn.note_tool_call("query_product", json.dumps({"error": "工具执行出错: boom"}, ensure_ascii=False))

    assert turn.tool_calls[0] == {"name": "query_order", "ok": True, "error": None}
    assert turn.tool_calls[1] == {"name": "apply_refund", "ok": False, "error": "订单不存在"}
    assert turn.tool_calls[2]["ok"] is False


def test_non_json_result_treated_as_ok():
    """工具结果不是 JSON 时无法判定成败,按成功计(不制造假失败)。"""
    turn = SkillTurn()
    turn.note_tool_call("search_knowledge", "一段纯文本检索结果")
    assert turn.tool_calls[0]["ok"] is True


def test_outcome_handoff_wins_over_tool_error():
    turn = SkillTurn()
    turn.note_tool_call("apply_refund", json.dumps({"success": False, "error": "x"}, ensure_ascii=False))
    assert turn.outcome(requires_human=True) == OUTCOME_HANDOFF


def test_outcome_tool_error_when_any_tool_failed():
    turn = SkillTurn()
    turn.note_tool_call("query_order", json.dumps({"success": True}, ensure_ascii=False))
    turn.note_tool_call("apply_refund", json.dumps({"success": False, "error": "x"}, ensure_ascii=False))
    assert turn.outcome(requires_human=False) == OUTCOME_TOOL_ERROR


def test_outcome_success_when_all_tools_ok():
    turn = SkillTurn()
    turn.note_tool_call("query_order", json.dumps({"success": True}, ensure_ascii=False))
    assert turn.outcome(requires_human=False) == OUTCOME_SUCCESS


# ---------- 埋点接线:回合结束落库(不跑真 LLM,直接调 _record_skill_turn) ----------

def test_record_skill_turn_writes_trace(tmp_path, monkeypatch):
    from app.agent.chat import EcomAgent
    from app.config.settings import settings
    from app.db import Database
    from app.schemas.response import CustomerServiceResponse, IntentType

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_trace_enabled", True)

    agent = EcomAgent.__new__(EcomAgent)          # 不跑 __init__,只测埋点方法
    agent.session_id = "s-trace"
    agent.user_id = "u-trace"
    agent._skill_turn = SkillTurn()
    agent._skill_turn.note_tool_call("load_skill", _load_skill_ok("track-order"))
    agent._skill_turn.note_tool_call("query_logistics",
                                     json.dumps({"success": True}, ensure_ascii=False))

    result = CustomerServiceResponse(intent=IntentType.OTHER, confidence=1.0,
                                     reply="已查到物流", requires_human=False,
                                     follow_up_question=None)
    agent._record_skill_turn(result)

    rows = db.list_skill_traces()
    assert len(rows) == 1
    assert rows[0]["skill_name"] == "track-order"
    assert rows[0]["outcome"] == "success"
    assert [c["name"] for c in rows[0]["tool_calls"]] == ["load_skill", "query_logistics"]


def test_record_skill_turn_noop_without_skill(tmp_path, monkeypatch):
    from app.agent.chat import EcomAgent
    from app.config.settings import settings
    from app.db import Database
    from app.schemas.response import CustomerServiceResponse, IntentType

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_trace_enabled", True)

    agent = EcomAgent.__new__(EcomAgent)
    agent.session_id = "s-none"
    agent.user_id = "u-none"
    agent._skill_turn = SkillTurn()
    agent._skill_turn.note_tool_call("query_order", json.dumps({"success": True}, ensure_ascii=False))

    agent._record_skill_turn(CustomerServiceResponse(
        intent=IntentType.OTHER, confidence=1.0, reply="ok",
        requires_human=False, follow_up_question=None))

    assert db.list_skill_traces() == []   # 未加载 skill 的轮次不落库
