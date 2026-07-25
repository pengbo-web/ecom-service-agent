"""空闲会话自动巩固:SessionManager.sweep 用可注入时钟,不起真线程。"""

from app.api.session_manager import SessionManager


class FakeAgent:
    def __init__(self, session_path):
        self.session_path = session_path
        self.saved = 0
        self.closed = 0

    def save(self):
        self.saved += 1

    def close(self):
        self.closed += 1   # 真实里 close() → 巩固长期记忆


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _mgr():
    clock = Clock()
    return SessionManager(agent_factory=lambda p, u=None: FakeAgent(p), clock=clock), clock


def test_active_session_not_reaped():
    mgr, clock = _mgr()
    mgr.get_or_create("s1")          # 活跃于 t=1000
    clock.t = 1000 + 100             # 才过 100s
    assert mgr.sweep(idle_ttl=300) == []
    assert "s1" in mgr._agents


def test_idle_session_consolidated_and_evicted():
    mgr, clock = _mgr()
    agent = mgr.get_or_create("s1")  # t=1000
    clock.t = 1000 + 301             # 空闲超过 300s
    reaped = mgr.sweep(idle_ttl=300)
    assert reaped == ["s1"]
    assert agent.closed == 1 and agent.saved == 1   # 巩固(close)+保存都发生
    assert "s1" not in mgr._agents                   # 已从内存回收


def test_activity_refreshes_idle_timer():
    mgr, clock = _mgr()
    mgr.get_or_create("s1")          # t=1000
    clock.t = 1250
    mgr.get_or_create("s1")          # 再次访问刷新活跃时间 → t=1250
    clock.t = 1400                   # 距上次活跃仅 150s
    assert mgr.sweep(idle_ttl=300) == []


def test_reaped_session_reloads_on_return():
    mgr, clock = _mgr()
    mgr.get_or_create("s1")
    clock.t += 999
    mgr.sweep(idle_ttl=300)
    assert "s1" not in mgr._agents
    again = mgr.get_or_create("s1")  # 用户回来 → 重新装载(从磁盘 session 文件恢复)
    assert again is not None and "s1" in mgr._agents


def test_only_idle_ones_reaped_mixed():
    mgr, clock = _mgr()
    mgr.get_or_create("old")         # t=1000
    clock.t = 1200
    mgr.get_or_create("fresh")       # t=1200
    clock.t = 1400                   # old 空闲 400s,fresh 空闲 200s
    assert mgr.sweep(idle_ttl=300) == ["old"]
    assert "old" not in mgr._agents and "fresh" in mgr._agents


def test_reaper_keeps_conversation_open_by_default(tmp_path, monkeypatch):
    """默认不自动结束会话:空闲回收后会话仍 open(下次打开复用原会话)。"""
    from app.db import Database, get_db
    from app.config.settings import settings
    temp_db = Database(str(tmp_path / "t.db"))
    temp_db.init_schema()
    monkeypatch.setattr("app.db._DB", temp_db)
    monkeypatch.setattr(settings, "conversation_idle_close_enabled", False)

    cid = temp_db.create_conversation("u1")["conversation_id"]
    mgr, clock = _mgr()
    mgr.get_or_create(cid)
    clock.t = 1000 + 301
    reaped = mgr.sweep(idle_ttl=300)
    assert reaped == [cid]                                    # 仍巩固+回收内存
    assert get_db().get_conversation(cid)["status"] == "open" # 但会话不结束
    # open_or_reuse 会复用它(每次打开还是原会话)
    from app.api.conversations import open_or_reuse
    assert open_or_reuse(get_db(), "u1")["conversation_id"] == cid


def test_reaper_closes_conversation_when_gated_on(tmp_path, monkeypatch):
    """开 conversation_idle_close_enabled 时:空闲回收置 closed(工单式翻篇)。"""
    from app.db import Database, get_db
    from app.config.settings import settings
    temp_db = Database(str(tmp_path / "t.db"))
    temp_db.init_schema()
    monkeypatch.setattr("app.db._DB", temp_db)
    monkeypatch.setattr(settings, "conversation_idle_close_enabled", True)

    cid = temp_db.create_conversation("u1")["conversation_id"]
    mgr, clock = _mgr()
    mgr.get_or_create(cid)
    clock.t = 1000 + 301
    mgr.sweep(idle_ttl=300)
    assert get_db().get_conversation(cid)["close_reason"] == "idle"
