"""按 key（会话）滑动窗口限流。"""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_per_window: int, window_seconds: int = 60, now=time.time):
        self.max = max_per_window
        self.window = window_seconds
        self._now = now
        self._hits = defaultdict(deque)   # key -> 时间戳队列

    def allow(self, key: str) -> bool:
        now = self._now()
        dq = self._hits[key]
        while dq and now - dq[0] >= self.window:
            dq.popleft()
        if len(dq) >= self.max:
            return False
        dq.append(now)
        return True
