"""Phase 7b:配置签名式热更新——重读配置、比对变化、应用到在运行的对象,免重启。"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config.settings import settings
from app.config.hot_reload import config_signature, reload_settings, HOT_FIELDS
from app.api.app import create_app
from app.api.session_manager import SessionManager


@pytest.fixture(autouse=True)
def restore_settings():
    """快照热更字段,测试后还原,避免污染全局单例。"""
    snap = {f: getattr(settings, f, None) for f in HOT_FIELDS}
    yield
    for f, v in snap.items():
        setattr(settings, f, v)


def test_signature_covers_hot_fields():
    sig = config_signature()
    assert set(sig.keys()) == set(HOT_FIELDS)


def test_reload_detects_and_applies_changes():
    fresh = SimpleNamespace(**{f: getattr(settings, f) for f in HOT_FIELDS})
    fresh.rate_limit_per_min = getattr(settings, "rate_limit_per_min") + 100
    fresh.hitl_confidence_threshold = 0.123
    changed = reload_settings(fresh=fresh)
    assert set(changed) == {"rate_limit_per_min", "hitl_confidence_threshold"}
    assert settings.rate_limit_per_min == fresh.rate_limit_per_min   # 原地生效
    assert settings.hitl_confidence_threshold == 0.123


def test_reload_no_change_returns_empty():
    fresh = SimpleNamespace(**{f: getattr(settings, f) for f in HOT_FIELDS})
    assert reload_settings(fresh=fresh) == []


class FakeAgent:
    def __init__(self, p):
        self.session_path = p


def test_reload_endpoint_applies_to_rate_limiter(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", str(getattr(settings, "rate_limit_per_min") + 50))
    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p))
    client = TestClient(create_app(session_manager=mgr))
    body = client.post("/api/config/reload").json()
    assert "rate_limit_per_min" in body["changed"]
    assert "rate_limit_per_min" in body["applied"]
