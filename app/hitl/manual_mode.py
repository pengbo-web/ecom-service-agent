"""人工接管模式（内存态 + 超时自动回落）。借鉴 XianyuAutoAgent 的接管开关。"""

import time


class ManualMode:
    def __init__(self, timeout: int = 3600, now=time.time):
        self.timeout = timeout
        self._now = now
        self._entered: dict = {}   # session_id -> 进入时间

    def enter(self, session_id: str) -> None:
        self._entered[session_id] = self._now()

    def exit(self, session_id: str) -> None:
        self._entered.pop(session_id, None)

    def is_manual(self, session_id: str) -> bool:
        ts = self._entered.get(session_id)
        if ts is None:
            return False
        if self._now() - ts > self.timeout:
            self.exit(session_id)     # 超时自动回落自动模式
            return False
        return True

    def toggle(self, session_id: str) -> str:
        if self.is_manual(session_id):
            self.exit(session_id)
            return "auto"
        self.enter(session_id)
        return "manual"
