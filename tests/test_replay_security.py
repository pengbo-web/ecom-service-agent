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
