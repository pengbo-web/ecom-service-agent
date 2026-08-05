"""客服转人工旁路信号:只在**最终**判定转人工时发、fail-soft、不改买家这一轮的返回。

发布点在 app/api/streaming.py 的 _normal_flow——而不是 EcomAgent.chat() 内部,
理由是"是否转人工"这件事在 chat() 返回之后才终局:HITL 规则(重复提问/低置信度/
敏感意图/关键词)会在 _normal_flow 里事后追加升级,chat() 自己完全不知道这些规则
判过什么。旧版把埋点放在 chat() 里、只看模型自报的 requires_human,漏掉了这一大类
真实转人工(defect A);同时评估沙箱(app/evaluation/sandbox.py)直接调 agent.chat()
不经这里,天然不再发出没有真实 session_id/user_id 的合成信号(defect B)。

fixture 用真实的 HitlManager + HandoffQueue(与 tests/test_streaming_handoff.py
同一套手法),让"HITL 规则单独判定升级"这条测试走的是真实 should_escalate 逻辑,
而不是自己 mock 出一个永远返回预设值的假 evaluate——否则测试只是在验证测试
自己写的桩,证明不了生产代码里的规则真的接上了。
"""

import itertools
from unittest.mock import patch

import pytest

from app.api.streaming import run_agent_streaming
from app.db.database import Database
from app.hitl.manager import HitlManager
from app.hitl.manual_mode import ManualMode
from app.hitl.queue import HandoffQueue
from app.multi_agent import bus
from app.schemas.response import CustomerServiceResponse, IntentType


class FakeAgent:
    """模拟 EcomAgent:chat() 直接返回预置结果,不触发真 LLM。"""

    def __init__(self, intent=IntentType.ORDER_QUERY, confidence=0.9,
                 requires_human=False, reply="已为您处理"):
        self.event_sink = None
        self.client = None
        self.user_id = "buyer-42"
        self.raw_messages = [{"role": "user", "content": "上一轮"}]
        self._turn_qu = None
        self._pending = None
        self._r = CustomerServiceResponse(
            intent=intent, confidence=confidence, reply=reply,
            requires_human=requires_human, follow_up_question=None,
        )

    def chat(self, user_input):
        return self._r


@pytest.fixture()
def hitl(tmp_path):
    """confidence_threshold=0.6:低于此值的自报置信度会被规则判定为需要转人工。"""
    ids = itertools.count(1)
    q = HandoffQueue(str(tmp_path / "h.db"), id_factory=lambda: f"h{next(ids)}")
    q.init_schema()
    return HitlManager(q, ManualMode(3600), confidence_threshold=0.6)


@pytest.fixture(autouse=True)
def _no_ticket_writes(monkeypatch):
    """升级路径会记工单,测试环境不需要真的落盘,静默掉即可。"""
    monkeypatch.setattr("app.agent.memory.profile.record_ticket", lambda *a, **k: None)


def _payload_of(pub) -> dict:
    call = pub.call_args
    return call[0][1] if len(call[0]) > 1 else call[1]["payload"]


def test_hitl_only_escalation_publishes_signal(hitl):
    """模型自己判定不需要转人工(requires_human=False),但置信度低于阈值,
    HITL 规则事后把它升级——这正是修复前静默失效的那类escalation。"""
    agent = FakeAgent(intent=IntentType.PRODUCT_CONSULT, confidence=0.3,
                      requires_human=False)
    with patch("app.multi_agent.bus.publish") as pub:
        events = list(run_agent_streaming(agent, "这个能便宜点吗", hitl=hitl,
                                          session_id="sess-real-1"))
    assert pub.called, "HITL 规则升级的转人工没有发出信号"
    payload = _payload_of(pub)
    assert payload["kind"] == "service_escalation"
    assert payload["session_id"] == "sess-real-1"
    assert payload["user_id"] == "buyer-42"
    assert payload["reasons"]   # 非空:确实是规则升级
    meta = [e for e in events if e["type"] == "metadata"][0]
    assert meta["requires_human"] is True   # metadata 侧也确实反映了事后升级


def test_self_reported_escalation_still_publishes(hitl):
    """模型自己判定需要转人工,HITL 规则没有再额外加别的原因——依旧要发信号。

    should_escalate 会把"模型判定需转人工"这条原因也计入 reasons(它如实
    转述了 requires_human 这个输入,不是凭空发明的规则),所以这里断言的是
    reasons 里**只有**这一条、没有任何规则自己触发的原因(低置信度/重复
    提问/敏感意图等)——这才是"模型自报"和"规则事后强制"的真正区别所在。
    """
    agent = FakeAgent(intent=IntentType.RETURN_REQUEST, confidence=0.95,
                      requires_human=True)
    with patch("app.multi_agent.bus.publish") as pub:
        list(run_agent_streaming(agent, "我要退款", hitl=hitl, session_id="sess-real-2"))
    assert pub.called
    payload = _payload_of(pub)
    assert payload["kind"] == "service_escalation"
    assert payload["session_id"] == "sess-real-2"
    assert payload["reasons"] == ["模型判定需转人工"]   # 仅模型自报,规则未额外升级


def test_ordinary_turn_publishes_nothing(hitl):
    agent = FakeAgent(intent=IntentType.ORDER_QUERY, confidence=0.95,
                      requires_human=False)
    with patch("app.multi_agent.bus.publish") as pub:
        list(run_agent_streaming(agent, "我的订单到哪了", hitl=hitl, session_id="sess-real-3"))
    pub.assert_not_called()


def test_bus_failure_does_not_propagate_to_caller(hitl):
    """总线挂掉不能让买家那一轮的回复/流式响应受影响。"""
    agent = FakeAgent(intent=IntentType.COMPLAINT, confidence=0.95, requires_human=True)
    with patch("app.multi_agent.bus.publish", side_effect=RuntimeError("bus down")):
        events = list(run_agent_streaming(agent, "投诉", hitl=hitl, session_id="sess-real-4"))
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "error" not in types
    assert "reply" in types and "metadata" in types


def test_signal_payload_carries_real_session_id_not_empty(hitl):
    """对照 defect B(评估沙箱发出 session_id/subject 为空串的信号):生产路径
    发出的信号必须带真实 session_id,而不是空串。"""
    agent = FakeAgent(intent=IntentType.COMPLAINT, confidence=0.95, requires_human=True)
    with patch("app.multi_agent.bus.publish") as pub:
        list(run_agent_streaming(agent, "投诉", hitl=hitl, session_id="sess-not-empty"))
    payload = _payload_of(pub)
    assert payload["session_id"] == "sess-not-empty"
    assert payload["session_id"] != ""
    assert payload["subject"] == "sess-not-empty"


@pytest.fixture()
def wired_db(tmp_path, monkeypatch):
    """真实 Database + 真实 bus.publish(不 mock),用于端到端验证开关生效。"""
    d = Database(db_path=str(tmp_path / "bus.db"))
    d.init_schema()
    monkeypatch.setattr(bus, "get_db", lambda: d)
    return d


def test_disabled_switch_publishes_nothing_end_to_end(hitl, wired_db, monkeypatch):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "collab_enabled", False)
    agent = FakeAgent(intent=IntentType.COMPLAINT, confidence=0.95, requires_human=True)
    list(run_agent_streaming(agent, "投诉", hitl=hitl, session_id="sess-switch"))
    assert wired_db.list_events() == []
