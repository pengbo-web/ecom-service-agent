"""快路径("你好"等)的问答也要写进会话历史,刷新/切换后可回显(此前走快路径不落盘 → 丢失)。"""

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.agent.storage import save_session


class FakeAgent:
    """最小 agent:raw_messages + 真实落盘 save(供历史接口从文件读回)。"""
    def __init__(self, session_path):
        self.session_path = session_path
        self.raw_messages = []

    def save(self):
        save_session(self.session_path, self.raw_messages, None)


def _client(tmp_path):
    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p), base_dir=str(tmp_path))
    return TestClient(create_app(session_manager=mgr)), mgr


def _drain(resp):  # 消费 SSE 流
    return resp.text


def test_fast_path_turn_persisted_and_in_history(tmp_path):
    from app.db import get_db
    client, mgr = _client(tmp_path)
    # 先建一个属于 u1 的真实 open 会话,否则 ensure_active 会把未知 session_id 换发成新会话,
    # 落盘随之落到换发后的 id 上(此测试关注的是落盘本身,不是换发)。
    cid = get_db().create_conversation("u1")["conversation_id"]
    # 发一句命中快路径的"你好"
    r = client.post("/api/chat", json={"session_id": cid, "user_id": "u1", "message": "你好"})
    assert r.status_code == 200
    _drain(r)

    # 已写入 agent 历史(用户 + 助手两条)
    agent = mgr.get_or_create(cid)
    assert len(agent.raw_messages) == 2
    assert agent.raw_messages[0] == {"role": "user", "content": "你好"}

    # 历史接口能回显(模拟切走再回来 / 刷新)
    turns = client.get(f"/api/session/{cid}/history").json()["turns"]
    assert turns[0] == {"role": "user", "content": "你好"}
    assert turns[1]["role"] == "assistant" and "小夕" in turns[1]["content"]


def test_non_fast_path_message_not_short_circuited(tmp_path):
    # 非寒暄消息不命中快路径,不在此分支落盘(交给 Agent 流程)——这里只验证快路径未误吞
    from app.hardening.fast_path import match_fast_path
    assert match_fast_path("查一下订单 ORD-1") is None
    assert match_fast_path("你好") is not None
