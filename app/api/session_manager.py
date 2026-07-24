"""按 session_id 管理独立的 Agent 实例与会话锁 + 空闲会话自动巩固记忆。

真实客服场景里长期记忆不是手动触发的:Web 无"会话结束"信号(用户直接关页面),
因此用**空闲超时**近似——后台线程定期扫描,空闲超过 TTL 的会话自动 close()
(触发长期记忆巩固/策展)并从内存回收,与 CLI 退出时 agent.close() 的效果一致。
"""

import threading
import time
from pathlib import Path
from typing import Callable, Optional


def _default_factory(session_path: str, user_id: str | None = None):
    # H1.0-C:总控 Agent(MultiAgentOrchestrator)是系统唯一入口——不再有单 Agent 运行模式。
    # (settings.multi_agent_enabled 已废弃,恒当 True;此处不再分支判断。)
    from app.multi_agent.orchestrator import MultiAgentOrchestrator
    return MultiAgentOrchestrator(session_path=session_path, user_id=user_id)


class SessionManager:
    def __init__(self, agent_factory=None, base_dir: str = "app/sessions/api",
                 clock: Optional[Callable[[], float]] = None, archiver=None):
        self._factory = agent_factory or _default_factory
        self._base_dir = Path(base_dir)
        if archiver is None:
            from app.session.archive import NullSessionArchiver
            archiver = NullSessionArchiver()
        self._archiver = archiver
        self._agents: dict = {}
        self._locks: dict = {}
        self._last_active: dict[str, float] = {}
        self._clock = clock or time.monotonic       # 单调时钟测空闲,可注入便于测试
        self._guard = threading.Lock()  # 保护字典本身
        self._reaper_stop: Optional[threading.Event] = None
        self._reaper_thread: Optional[threading.Thread] = None

    def _session_path(self, session_id: str) -> str:
        return str(self._base_dir / f"{session_id}.json")

    def get_or_create(self, session_id: str, user_id: str | None = None):
        with self._guard:
            if session_id not in self._agents:
                self._agents[session_id] = self._factory(self._session_path(session_id), user_id)
            self._last_active[session_id] = self._clock()   # 任何访问都算活跃
            return self._agents[session_id]

    def peek_messages(self, session_id: str) -> list:
        """只读取该会话已存的原始消息(不创建 agent),供历史回显。"""
        from app.session.store import get_session_store
        state = get_session_store().load(self._session_path(session_id))
        return state.get("messages", []) if state else []

    def get_lock(self, session_id: str) -> threading.Lock:
        with self._guard:
            if session_id not in self._locks:
                self._locks[session_id] = threading.Lock()
            return self._locks[session_id]

    def reset(self, session_id: str) -> None:
        with self._guard:
            agent = self._agents.pop(session_id, None)
            self._last_active.pop(session_id, None)
        if agent is not None and hasattr(agent, "reset"):
            agent.reset()
        try:
            from app.db import get_db
            get_db().clear_bargain_state(session_id)
        except Exception:
            pass

    def sweep(self, idle_ttl: float) -> list[str]:
        """把空闲(距最后活跃 >= idle_ttl)的会话巩固记忆并从内存回收,返回被回收的 session_id。"""
        now = self._clock()
        with self._guard:
            stale = [sid for sid, t in self._last_active.items() if now - t >= idle_ttl]
        reaped = []
        for sid in stale:
            self._consolidate_and_evict(sid)
            reaped.append(sid)
        return reaped

    def _consolidate_and_evict(self, session_id: str) -> None:
        """串行地保存+close(巩固长期记忆)该会话,再从内存移除;失败不影响其它会话。"""
        lock = self.get_lock(session_id)
        with lock:
            with self._guard:
                agent = self._agents.get(session_id)
            if agent is not None:
                try:
                    if hasattr(agent, "save"):
                        agent.save()
                    if hasattr(agent, "close"):
                        agent.close()   # → memory_manager.consolidate_to_long_term(...)
                except Exception:
                    pass
                self._archiver.archive(session_id, agent)   # 冷归档(best-effort)
            with self._guard:
                self._agents.pop(session_id, None)
                self._last_active.pop(session_id, None)

    def start_reaper(self, interval: float, idle_ttl: float) -> None:
        """启动后台守护线程,每 interval 秒扫一次,回收空闲超过 idle_ttl 的会话。幂等。"""
        if self._reaper_stop is not None:
            return
        self._reaper_stop = threading.Event()

        def loop():
            while not self._reaper_stop.wait(interval):
                try:
                    reaped = self.sweep(idle_ttl)
                    if reaped:
                        print(f"🧠 [自动巩固] 空闲会话 {reaped} 已巩固长期记忆并回收", flush=True)
                except Exception:
                    pass

        self._reaper_thread = threading.Thread(target=loop, daemon=True, name="session-reaper")
        self._reaper_thread.start()

    def stop_reaper(self) -> None:
        if self._reaper_stop is not None:
            self._reaper_stop.set()
