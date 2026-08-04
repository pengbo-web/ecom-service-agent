"""卖家会话 API:鉴权、开关、会话隔离、路由结果透出。"""

import pytest
from fastapi.testclient import TestClient

from app.multi_agent.agents import SELLER_AGENT_CONFIGS


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")
    from app.api.app import create_app
    return TestClient(create_app())


# 管理鉴权走 X-Admin-Token(见 app/hardening/auth.py::make_admin_auth),不是
# Authorization: Bearer —— 与 tests/test_skill_admin_api.py 等既有 admin 端点
# 测试用的头一致。
AUTH = {"X-Admin-Token": "T"}


def test_seller_chat_requires_auth(client):
    r = client.post("/api/seller/chat", json={"session_id": "s1", "message": "近7天GMV"})
    assert r.status_code in (401, 403)


def test_seller_overview_requires_auth(client):
    assert client.get("/api/seller/overview").status_code in (401, 403)


def test_seller_overview_returns_metrics_and_anomalies(client):
    r = client.get("/api/seller/overview?window_days=7", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "overview" in body and "anomalies" in body
    assert body["overview"]["success"] is True


def test_seller_chat_returns_agent_key(client, monkeypatch):
    """本轮由哪个画像作答,必须**读编排器的真实结果**,不能是端点兜底猜的。

    这里刻意用 growth 而不是 analyst:analyst 恰好是端点取不到属性时的兜底值,
    用它做断言就分不清"真读到了"还是"兜底成了同一个值"——测试会失去可失败性。
    """
    from app.api import app as appmod

    class FakeOrch:
        last_agent_key = "growth"
        def chat(self, text):
            return {"reply": f"收到:{text}", "requires_human": False}
        def save(self):
            pass

    monkeypatch.setattr(appmod.seller_sessions, "get_or_create",
                        lambda sid, user_id=None: FakeOrch())
    r = client.post("/api/seller/chat",
                    json={"session_id": "s1", "message": "有哪些下单没推进的?"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["agent_key"] == "growth"
    assert body["agent"] == SELLER_AGENT_CONFIGS["growth"]["name"]
    assert "收到" in body["reply"]


def test_seller_chat_404_when_console_disabled(client, monkeypatch):
    """控制台关掉时聊天端点也必须是 404(此前只测了 overview 那一半)。"""
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "seller_console_enabled", False)
    r = client.post("/api/seller/chat",
                    json={"session_id": "s1", "message": "x"}, headers=AUTH)
    assert r.status_code == 404


def test_seller_sessions_are_isolated_from_buyer_sessions(client):
    """店主和买家用同一个 session_id 也不能串——两套 SessionManager 各自独立。

    买家侧的 SessionManager 是 create_app() 内部的局部变量(未注入时新建、
    可注入以便测试隔离),不是模块级名字,因此不能直接
    `from app.api.app import sessions` 拿到它——真实拿法是从这条 client 背后
    那个具体的 app 实例上取(create_app() 里已把它挂到 app.state.session_manager,
    专门供这类内省使用)。这里比对的正是"这次请求实际会用到的买家会话管理器"
    与卖家会话管理器,而不是凭空构造的另一个实例。
    """
    from app.api.app import seller_sessions
    buyer_sessions = client.app.state.session_manager
    assert seller_sessions is not buyer_sessions
    assert seller_sessions._base_dir != buyer_sessions._base_dir


def test_disabled_console_returns_404(client, monkeypatch):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "seller_console_enabled", False)
    r = client.get("/api/seller/overview", headers=AUTH)
    assert r.status_code == 404
