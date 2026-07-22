"""/api/session/{id}/consolidate:手动触发记忆巩固并回传长期记忆事实。"""

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager


class _LTM:
    def __init__(self):
        self.facts = []


class _MM:
    memory_enabled = True

    def __init__(self):
        self.ltm = _LTM()
        self.consolidated = None

    def consolidate_to_long_term(self, messages, summary):
        self.consolidated = (list(messages), summary)
        # 模拟策展后的干净结果
        self.ltm.facts = [type("F", (), {"content": "偏好红色系", "category": "preference",
                                         "created_at": "2023-01-01T00:00:00"})()]


class FakeAgent:
    def __init__(self, session_path):
        self.session_path = session_path
        self.raw_messages = [{"role": "user", "content": "我喜欢红色"}]
        self.summary = None
        self.memory_manager = _MM()


class NoMemAgent:
    def __init__(self, session_path):
        self.session_path = session_path
        self.memory_manager = None


def test_consolidate_returns_curated_facts():
    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p))
    client = TestClient(create_app(session_manager=mgr))
    r = client.post("/api/session/s1/consolidate")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["count"] == 1
    assert body["facts"][0]["content"] == "偏好红色系"
    # 确实把会话消息传给了巩固逻辑
    agent = mgr.get_or_create("s1")
    assert agent.memory_manager.consolidated[0] == [{"role": "user", "content": "我喜欢红色"}]


def test_consolidate_when_memory_disabled():
    mgr = SessionManager(agent_factory=lambda p, u=None: NoMemAgent(p))
    client = TestClient(create_app(session_manager=mgr))
    body = client.post("/api/session/s2/consolidate").json()
    assert body == {"enabled": False, "count": 0, "facts": []}
