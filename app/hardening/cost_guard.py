"""按天请求预算，成本兜底（防刷爆 API Key）。"""

import threading
import time


class CostGuard:
    def __init__(self, max_requests_per_day: int, now=time.time):
        self.max = max_requests_per_day
        self._now = now
        self._day = None
        self._count = 0
        # 线程池并发下 _roll + 自增无锁会多扣/漏扣,预算上限不可靠
        self._lock = threading.Lock()

    def _today(self) -> int:
        return int(self._now() // 86400)

    def _roll(self) -> None:
        d = self._today()
        if d != self._day:
            self._day = d
            self._count = 0

    def allow(self) -> bool:
        with self._lock:
            self._roll()
            if self._count >= self.max:
                return False
            self._count += 1
            return True

    def spent(self) -> int:
        with self._lock:
            self._roll()
            return self._count
