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
