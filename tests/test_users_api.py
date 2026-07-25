"""用户创建/登录/me。auth 专项测试自行开启门控(conftest 默认关)。"""

import re
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.config.settings import settings
from app.db import Database


class FakeAgent:
    def __init__(self, session_path):
        self.raw_messages = []
    def chat(self, user_input):
        raise AssertionError("本文件不该触发对话")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    monkeypatch.setattr("app.api.app.get_db", lambda: db)
    monkeypatch.setattr(settings, "auth_enabled", True)
    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p))
    return TestClient(create_app(session_manager=mgr))


def test_create_user_returns_token(client):
    r = client.post("/api/users", json={"user_id": "小明", "name": "小明"})
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "小明" and body["token"].count(".") == 1


def test_create_duplicate_409(client):
    client.post("/api/users", json={"user_id": "u1", "name": "u1"})
    assert client.post("/api/users", json={"user_id": "u1", "name": "x"}).status_code == 409


def test_create_invalid_id_422(client):
    assert client.post("/api/users", json={"user_id": "bad id!", "name": "x"}).status_code == 422
    assert client.post("/api/users", json={"user_id": "a" * 33, "name": "x"}).status_code == 422


def test_login_existing_and_missing(client):
    client.post("/api/users", json={"user_id": "小红", "name": "小红"})
    ok = client.post("/api/auth/login", json={"user_id": "小红"})
    assert ok.status_code == 200 and ok.json()["token"]
    assert client.post("/api/auth/login", json={"user_id": "ghost"}).status_code == 404


def test_me_roundtrip_and_401(client):
    token = client.post("/api/users", json={"user_id": "u2", "name": "u2"}).json()["token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["user_id"] == "u2"
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer bad.token"}).status_code == 401
