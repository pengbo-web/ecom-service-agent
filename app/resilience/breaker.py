"""熔断器（借鉴 nanobot fallback_provider.py:108-115）。

主模型连续失败达阈值 → 跳闸;冷却期内 allow()=False(直接走备用);
冷却结束半开:放一个探测请求,成功则复位,失败则重新跳闸。
"""

import time


class CircuitBreaker:
    def __init__(self, threshold: int = 3, cooldown: float = 60.0, now=time.time):
        self.threshold = threshold
        self.cooldown = cooldown
        self._now = now
        self._failures = 0
        self._opened_at: float | None = None

    def allow(self) -> bool:
        """当前是否允许尝试主模型。"""
        if self._opened_at is None:
            return True
        if self._now() - self._opened_at >= self.cooldown:
            return True          # 半开:放一个探测
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = self._now()

    @property
    def is_open(self) -> bool:
        return not self.allow()
