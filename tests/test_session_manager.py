import threading

from app.api.session_manager import SessionManager, _default_factory
from app.config.settings import settings
from app.multi_agent.orchestrator import MultiAgentOrchestrator


class FakeAgent:
    def __init__(self, session_path):
        self.session_path = session_path
        self.reset_called = False

    def reset(self):
        self.reset_called = True


def _mgr():
    return SessionManager(agent_factory=lambda p, u=None: FakeAgent(p))


def test_same_session_returns_same_instance():
    mgr = _mgr()
    a = mgr.get_or_create("s1")
    b = mgr.get_or_create("s1")
    assert a is b


def test_different_sessions_are_isolated():
    mgr = _mgr()
    a = mgr.get_or_create("s1")
    b = mgr.get_or_create("s2")
    assert a is not b
    assert a.session_path != b.session_path


def test_session_path_uses_session_id():
    mgr = _mgr()
    a = mgr.get_or_create("abc")
    assert "abc" in a.session_path


def test_get_lock_is_stable_per_session():
    mgr = _mgr()
    l1 = mgr.get_lock("s1")
    l2 = mgr.get_lock("s1")
    assert l1 is l2
    assert isinstance(l1, type(threading.Lock()))


def test_reset_calls_agent_reset_and_drops_instance():
    mgr = _mgr()
    a = mgr.get_or_create("s1")
    mgr.reset("s1")
    assert a.reset_called is True
    # reset 后再取应是新实例
    assert mgr.get_or_create("s1") is not a


def test_default_factory_derives_session_id(tmp_path):
    mgr = SessionManager(base_dir=str(tmp_path))
    agent = mgr.get_or_create("sess-xyz")
    # 默认工厂应把 session_id 透传给 EcomAgent
    assert getattr(agent, "session_id", None) == "sess-xyz"


def test_reset_clears_bargain_state(tmp_path):
    from app.db.database import Database
    from app.db import set_db
    db = Database(db_path=str(tmp_path / "t.db"))
    db.init_schema()
    set_db(db)
    db.bump_bargain_state("s1", "P1", 900.0)

    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p), base_dir=str(tmp_path))
    mgr.get_or_create("s1")
    mgr.reset("s1")
    assert db.get_bargain_state("s1", "P1") is None


# ---- H1.0-C:移除单 Agent 模式,默认工厂恒建总控 Agent ----
def test_default_factory_returns_orchestrator(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    agent = _default_factory(str(tmp_path / "s.json"), user_id="u1")
    assert isinstance(agent, MultiAgentOrchestrator)


def test_factory_orchestrator_even_when_flag_false(tmp_path, monkeypatch):
    """开关已废弃(恒当 True):即使显式置 False,仍返回总控 Agent。"""
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    monkeypatch.setattr(settings, "multi_agent_enabled", False)
    agent = _default_factory(str(tmp_path / "s.json"), user_id="u1")
    assert isinstance(agent, MultiAgentOrchestrator)


def test_session_manager_get_or_create_returns_orchestrator(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    monkeypatch.setattr(settings, "multi_agent_enabled", False)
    sm = SessionManager(base_dir=str(tmp_path / "api"))
    agent = sm.get_or_create("sess-1", user_id="u1")
    assert isinstance(agent, MultiAgentOrchestrator)
