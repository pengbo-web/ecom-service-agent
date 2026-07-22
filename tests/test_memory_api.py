"""GET /api/memory:只读返回磁盘上的长期记忆(不触发巩固)。"""

import json

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.config.settings import settings


class FakeAgent:
    def __init__(self, session_path):
        self.session_path = session_path


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    monkeypatch.setattr(settings, "memory_user_id", "default")
    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p))
    return TestClient(create_app(session_manager=mgr))


def test_memory_empty_when_no_file(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    body = client.get("/api/memory").json()
    assert body["count"] == 0 and body["facts"] == []


def test_memory_reads_persisted_facts(monkeypatch, tmp_path):
    (tmp_path / "default.json").write_text(json.dumps({
        "facts": [
            {"content": "偏好红色系", "category": "preference",
             "created_at": "2023-01-01T00:00:00", "source_session": "s1"},
            {"content": "是钻石会员", "category": "identity", "created_at": "2024-06-01T00:00:00"},
        ],
        "interaction_summaries": [{"summary": "咨询退货", "timestamp": "2024-06-01T10:00:00"}],
    }, ensure_ascii=False), encoding="utf-8")

    client = _client(monkeypatch, tmp_path)
    body = client.get("/api/memory").json()
    assert body["count"] == 2
    assert body["facts"][0]["content"] == "偏好红色系"
    assert body["facts"][0]["created_at"] == "2023-01-01T00:00:00"
    assert body["interaction_summaries"][-1]["summary"] == "咨询退货"
