"""会话级并发锁:保证同一会话不被并发处理(踩状态)。可插拔。

- LocalSessionLock:进程内 threading.Lock(单实例够用,等价原 SessionManager.get_lock)。
- RedisSessionLock:Redis `SET NX PX` 分布式锁 + Lua 安全释放(多实例才正确)——
  单实例的进程内锁在多机部署下失效(两个实例可同时处理同一会话)。

用法(上下文管理器,拿不到锁 yield False):
    with get_session_lock().guard(session_id) as got:
        if not got: ...稍候重试...
        else: ...处理...
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


class LocalSessionLock:
    """单实例:按 key 的进程内互斥锁。"""

    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    @contextmanager
    def guard(self, key: str, timeout: float = 30.0):
        lk = self._lock_for(key)
        got = lk.acquire(timeout=timeout)
        try:
            yield got
        finally:
            if got:
                lk.release()


class RedisSessionLock:
    """多实例:Redis 分布式锁。SET NX PX 抢锁,释放时校验 token 再 DEL(防误删别人的锁)。

    释放用 WATCH/MULTI 乐观事务(可移植:真 redis + fakeredis 都支持;等价于 Lua CAS-del)。
    """

    def __init__(self, client, ttl_ms: int = 30000, wait_timeout: float = 30.0,
                 retry_interval: float = 0.1, now=time.monotonic, sleep=time.sleep):
        self._r = client
        self._ttl_ms = ttl_ms            # 锁自动过期(防持有者崩溃后死锁)
        self._wait = wait_timeout        # 抢不到时最多等多久
        self._retry = retry_interval
        self._now = now
        self._sleep = sleep

    @staticmethod
    def _lock_key(key: str) -> str:
        return f"lock:{Path(key).stem}"   # 与 sess:{stem} 对齐

    @contextmanager
    def guard(self, key: str, timeout: Optional[float] = None):
        rk = self._lock_key(key)
        token = uuid.uuid4().hex
        deadline = self._now() + (self._wait if timeout is None else timeout)
        acquired = False
        while True:
            if self._r.set(rk, token, nx=True, px=self._ttl_ms):
                acquired = True
                break
            if self._now() >= deadline:
                break
            self._sleep(self._retry)
        try:
            yield acquired
        finally:
            if acquired:
                self._release(rk, token)

    def _release(self, rk: str, token: str) -> None:
        """只删自己持有的锁(token 匹配才删),防误删别人的锁;失败靠 TTL 兜底。"""
        tok = token.encode()
        try:
            with self._r.pipeline() as pipe:
                pipe.watch(rk)
                cur = pipe.get(rk)
                if cur == tok or cur == token:
                    pipe.multi()
                    pipe.delete(rk)
                    pipe.execute()
                else:
                    pipe.unwatch()
        except Exception:  # noqa: BLE001 释放竞态/异常靠 TTL 兜底
            pass


# ---- 全局单例 + 工厂 ----
_lock = None


def get_session_lock():
    global _lock
    if _lock is None:
        _lock = _build_from_settings()
    return _lock


def set_session_lock(lock) -> None:
    global _lock
    _lock = lock


def _build_from_settings():
    from app.config.settings import settings
    if getattr(settings, "session_store_backend", "file") == "redis":
        import redis
        return RedisSessionLock(redis.from_url(settings.redis_url),
                                ttl_ms=settings.session_lock_ms)
    return LocalSessionLock()
