"""用户隔离(长期记忆按 user_id)+ 历史回显(GET /api/session/{id}/history)。"""

import json

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.api.history import reconstruct_bubbles
from app.agent.storage import save_session
from app.config.settings import settings


# ---- 历史重建(纯函数)----
def test_reconstruct_skips_internal_keeps_bubbles():
    reply_json = json.dumps({"intent": "greeting", "confidence": 0.9, "reply": "您好~"})
    messages = [
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "让我查一下", "tool_calls": [{"id": "1"}]},  # 中间步骤,跳过
        {"role": "tool", "tool_call_id": "1", "content": "{...}"},                     # 工具结果,跳过
        {"role": "assistant", "content": "查询完成的思考"},                             # 纯文本思考,跳过
        {"role": "assistant", "content": reply_json},                                   # 最终结构化回复,保留
    ]
    bubbles = reconstruct_bubbles(messages)
    assert bubbles == [
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "您好~"},
    ]


def test_reconstruct_empty():
    assert reconstruct_bubbles([]) == []
    assert reconstruct_bubbles(None) == []


# ---- 历史接口 ----
class FakeAgent:
    def __init__(self, p):
        self.session_path = p


def test_history_endpoint_returns_persisted_bubbles(tmp_path):
    sid = "sess-1"
    save_session(
        str(tmp_path / f"{sid}.json"),
        [
            {"role": "user", "content": "我要退款"},
            {"role": "assistant", "content": json.dumps({"reply": "好的,请确认订单号", "intent": "after_sale", "confidence": 0.9})},
        ],
        summary=None,
    )
    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p), base_dir=str(tmp_path))
    client = TestClient(create_app(session_manager=mgr))
    body = client.get(f"/api/session/{sid}/history").json()
    assert body["session_id"] == sid
    assert body["turns"] == [
        {"role": "user", "content": "我要退款"},
        {"role": "assistant", "content": "好的,请确认订单号"},
    ]


def test_history_endpoint_empty_for_unknown_session(tmp_path):
    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p), base_dir=str(tmp_path))
    client = TestClient(create_app(session_manager=mgr))
    assert client.get("/api/session/nobody/history").json()["turns"] == []


# ---- 用户隔离:/api/memory 按 user_id 读各自的档案 ----
def _write_ltm(dir_path, user_id, content):
    (dir_path / f"{user_id}.json").write_text(json.dumps({
        "facts": [{"content": content, "category": "preference", "created_at": "2024-01-01T00:00:00"}],
        "interaction_summaries": [],
    }, ensure_ascii=False), encoding="utf-8")


def test_memory_isolated_by_user(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    _write_ltm(tmp_path, "alice", "alice 偏好红色")
    _write_ltm(tmp_path, "bob", "bob 偏好蓝色")

    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p))
    client = TestClient(create_app(session_manager=mgr))

    alice = client.get("/api/memory?user_id=alice").json()
    bob = client.get("/api/memory?user_id=bob").json()
    assert alice["user_id"] == "alice" and alice["facts"][0]["content"] == "alice 偏好红色"
    assert bob["user_id"] == "bob" and bob["facts"][0]["content"] == "bob 偏好蓝色"
    # 互不串号
    assert alice["facts"][0]["content"] != bob["facts"][0]["content"]


def test_agent_binds_user_id_to_memory(tmp_path, monkeypatch):
    # EcomAgent(user_id=...) → 其 MemoryManager 的长期记忆按该 user_id 存
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    from app.agent.chat import EcomAgent
    a = EcomAgent(session_path=str(tmp_path / "s.json"), session_id="s", user_id="alice")
    assert a.user_id == "alice"
    assert a.memory_manager.ltm.user_id == "alice"
