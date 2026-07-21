"""Phase 4:确认轮由服务端确定性重放挂起动作(不经模型)。"""

import pytest

from app.db import Database, set_db
from app.db.seed import seed_from_mock
from app.agent.pending import PendingAction, PendingActionStore, set_pending_store
from app.agent.tools.order import query_order
from app.api.streaming import run_agent_streaming


@pytest.fixture(autouse=True)
def _db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.init_schema()
    seed_from_mock(d)
    set_db(d)
    return d


class FakeAgent:
    """只提供重放路径需要的最小接口;chat() 不应被调用(重放不经模型)。"""
    def __init__(self):
        self.raw_messages = []
        self.event_sink = None
        self.client = None
        self.session_id = "sess"
        self.saved = 0

    def save(self):
        self.saved += 1

    def chat(self, user_input):  # pragma: no cover - 重放路径不该走到这
        raise AssertionError("重放轮不应调用模型 chat()")


def _events(user_input):
    store = PendingActionStore()
    store.remember("sess", PendingAction(
        action="refund", tool_name="apply_refund",
        args={"order_id": "ORD-20240115-001", "reason": "尺码不合适"},
        message="请确认是否为订单 ORD-20240115-001 办理退款?",
    ))
    set_pending_store(store)
    agent = FakeAgent()
    evs = list(run_agent_streaming(agent, user_input, session_id="sess"))
    return evs, agent, store


def _reply(evs):
    return [e for e in evs if e["type"] == "reply"][0]["content"]


def test_confirm_replays_refund_deterministically():
    evs, agent, store = _events("确认,退款吧")
    # 工具真的被执行:订单状态落库为 refund_processing
    assert query_order("ORD-20240115-001")["order"]["status"] == "refund_processing"
    # 回复据真实结果生成,含成功标记,不谎报
    assert "✅" in _reply(evs)
    # 事件流里有重放的 tool_call
    assert any(e["type"] == "tool_call" and e["name"] == "apply_refund" for e in evs)
    # 挂起已清除,写回历史并保存
    assert store.get("sess") is None
    assert agent.saved == 1
    assert agent.raw_messages[-1]["role"] == "assistant"


def test_non_confirmation_does_not_replay():
    # 不是确认语 → 不重放:走正常模型流(FakeAgent.chat 抛错被 worker 吞成 error 事件),订单不被动
    store = PendingActionStore()
    store.remember("sess", PendingAction(
        "refund", "apply_refund",
        {"order_id": "ORD-20240115-001", "reason": "尺码不合适"}, "?"))
    set_pending_store(store)
    agent = FakeAgent()
    evs = list(run_agent_streaming(agent, "顺便问下有货吗", session_id="sess"))
    assert any(e.get("type") == "error" for e in evs)         # 走了正常流(没重放)
    assert store.get("sess") is not None                       # 挂起未被消费
    assert query_order("ORD-20240115-001")["order"]["status"] == "shipped"  # 订单未变


def test_confirm_without_pending_falls_through():
    # 说了确认语但没有挂起动作 → 不重放,走正常流(FakeAgent.chat 触发断言)
    set_pending_store(PendingActionStore())
    agent = FakeAgent()
    evs = list(run_agent_streaming(agent, "确认", session_id="sess"))
    # 正常流调用 chat() 抛错,被 worker 捕获为 error 事件
    assert any(e.get("type") == "error" for e in evs)
