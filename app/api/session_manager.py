"""按 session_id 管理独立的 Agent 实例与会话锁。"""

import threading
from pathlib import Path


def _default_factory(session_path: str):
    from app.agent.chat import EcomAgent
    return EcomAgent(session_path=session_path, session_id=Path(session_path).stem)


class SessionManager:
    def __init__(self, agent_factory=None, base_dir: str = "app/sessions/api"):
        self._factory = agent_factory or _default_factory
        self._base_dir = Path(base_dir)
        self._agents: dict = {}
        self._locks: dict = {}
        self._guard = threading.Lock()  # 保护字典本身

    def _session_path(self, session_id: str) -> str:
        return str(self._base_dir / f"{session_id}.json")

    def get_or_create(self, session_id: str):
        with self._guard:
            if session_id not in self._agents:
                self._agents[session_id] = self._factory(self._session_path(session_id))
            return self._agents[session_id]

    def get_lock(self, session_id: str) -> threading.Lock:
        with self._guard:
            if session_id not in self._locks:
                self._locks[session_id] = threading.Lock()
            return self._locks[session_id]

    def reset(self, session_id: str) -> None:
        with self._guard:
            agent = self._agents.pop(session_id, None)
        if agent is not None and hasattr(agent, "reset"):
            agent.reset()
        try:
            from app.db import get_db
            get_db().clear_bargain_state(session_id)
        except Exception:
            pass
