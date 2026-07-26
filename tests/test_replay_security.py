"""确认重放安全:auth 开启时重放必须带 user 身份且走 ToolManager(P0-1 + P1-③)。"""

import json
from types import SimpleNamespace

from app.api.streaming import run_agent_streaming
from app.agent.pending import PendingAction
from app.config.settings import settings


class FakeToolManager:
    def __init__(self):
        self.calls = []

    def execute_tool(self, name, arguments):
        # 记录调用时的 current_user——重放必须已设置身份
        from app.agent.runtime_context import get_current_user
        self.calls.append({"name": name, "args": arguments, "user": get_current_user()})
        return json.dumps({"success": True, "message": "退款已提交"}, ensure_ascii=False)


class FakeAgent:
    def __init__(self):
        self.raw_messages = []
        self.user_id = "u-real"
        self.tool_manager = FakeToolManager()
        self._pending = PendingAction(action="refund", tool_name="apply_refund",
                                      args={"order_id": "ORD-20240110-003", "reason": "不想要了"},
                                      message="请确认退款")
        self.client = None

    def save(self):
        pass

    def chat(self, user_input):
        from app.schemas.response import CustomerServiceResponse, IntentType
        return CustomerServiceResponse(intent=IntentType.OTHER, confidence=0.9,
                                       reply="好的", requires_human=False,
                                       follow_up_question=None)

    def set_turn_understanding(self, qu):
        self._turn_qu = qu

    _turn_qu = None


def _drain(agent, text, confirm=False):
    return list(run_agent_streaming(agent, text, session_id="s1", confirm=confirm))


def test_replay_sets_current_user_and_uses_tool_manager(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)   # 生产路径,必须显式开
    agent = FakeAgent()
    events = _drain(agent, "确认", confirm=True)
    tm = agent.tool_manager
    assert len(tm.calls) == 1                                  # 走了 ToolManager,不是直连 registry
    assert tm.calls[0]["name"] == "apply_refund"
    assert tm.calls[0]["user"] == "u-real"                     # 重放时 current_user 已设置(P0-1)
    reply = [e for e in events if e["type"] == "reply"][0]
    assert "退款" in reply["content"]
    assert agent._pending is None                             # 重放后清挂起


def _agent_with_pending(order="ORD-20240110-003"):
    a = FakeAgent()
    a._pending = PendingAction(action="refund", tool_name="apply_refund",
                               args={"order_id": order, "reason": "x"}, message="请确认退款")
    return a


def test_bare_affirmation_after_unrelated_question_does_not_replay(monkeypatch):
    """挂起退款后,agent 问了别的,用户'可以'不应重放退款(P0-2 场景A)。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    agent = _agent_with_pending()
    events = _drain(agent, "可以")           # 无 confirm 标志,只是泛化词
    assert agent.tool_manager.calls == []    # 没重放
    # 落到正常流(FakeAgent 无 chat 会怎样——见实现说明:测试用带 chat 的桩)


def test_explicit_confirm_flag_still_replays(monkeypatch):
    """前端显式 confirm=True 仍然重放(点击确认按钮的正规路径)。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    agent = _agent_with_pending()
    _drain(agent, "确认退款", confirm=True)
    assert len(agent.tool_manager.calls) == 1


def test_confirm_mentioning_target_order_replays(monkeypatch):
    """用户确认时提到了挂起单号 → 绑定成立,重放。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    agent = _agent_with_pending("ORD-20240110-003")
    _drain(agent, "确认对 ORD-20240110-003 退款")
    assert len(agent.tool_manager.calls) == 1


def test_confirm_wrong_order_does_not_replay_pending(monkeypatch):
    """挂起A单退款,用户说'确认取消订单B' → 不重放A单(P0-2 场景B)。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    agent = _agent_with_pending("ORD-20240110-003")
    _drain(agent, "确认取消订单 ORD-20240115-001")   # 提到的是别的单
    assert agent.tool_manager.calls == []
