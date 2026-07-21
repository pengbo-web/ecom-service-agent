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
    return SessionManager(agent_factory=lambda p: FakeAgent(p), clock=clock), clock


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
