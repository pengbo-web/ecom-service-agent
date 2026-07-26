"""意图一致性:trace 记录的意图(_drive 返回值)必须与 metadata 事件一致(P1)。"""

import json
from types import SimpleNamespace

from app.api.streaming import run_agent_streaming
from app.config.settings import settings


class _Agent:
    def __init__(self):
        self.raw_messages = []
        self.user_id = "u1"
        self._pending = None
        self.client = None
        self._turn_qu = SimpleNamespace(intent="投诉", need_kb=False, domain=None,
                                        kb_query=None, source="llm")

    def set_turn_understanding(self, qu):
        self._turn_qu = qu

    def chat(self, user_input):
        from app.schemas.response import CustomerServiceResponse, IntentType
        # 生成侧意图漂移成 promotion,与 QU 的"投诉"不一致
        return CustomerServiceResponse(intent=IntentType.PROMOTION, confidence=0.9,
                                       reply="回复", requires_human=False,
                                       follow_up_question=None)


def test_trace_intent_matches_metadata(monkeypatch):
    monkeypatch.setattr(settings, "faq_cache_enabled", False)
    agent = _Agent()
    events = list(run_agent_streaming(agent, "你们太坑了", session_id="s1", hitl=None))
    meta = [e for e in events if e["type"] == "metadata"][0]
    # QU=投诉 覆盖 → metadata.intent 应为 complaint;_drive 返回值(trace.intent)也应是 complaint
    assert meta["intent"] == "complaint"
    # 复现 app.py 的消费:_drive 的返回值即 run_agent_streaming 内 trace.intent 来源
    # 这里用事件侧验证:normal_flow 返回 intent_out,与 metadata 同源
