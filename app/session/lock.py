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

import logging
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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
                 retry_interval: float = 0.1, now=time.monotonic, sleep=time.sleep,
                 fallback=None, breaker=None):
        self._r = client
        self._ttl_ms = ttl_ms            # 锁自动过期(防持有者崩溃后死锁)
        self._wait = wait_timeout        # 抢不到时最多等多久
        self._retry = retry_interval
        self._now = now
        self._sleep = sleep
        #: Redis 不可用时退到的进程内锁。见 `guard` 的降级说明。
        self._fallback = fallback if fallback is not None else LocalSessionLock()
        #: 失败后退避。**这一处原先是漏的**:紧挨着的会话存储早就有退避
        #: (store.py 的 `_down_until`),这里只有 fail-soft 没有冷却,于是故障
        #: 期间每个请求都要重新连一次 Redis、再付一次 socket 超时。
        #:
        #: 本类的 `guard` docstring 里写着"同一次故障里,一半组件降级、另一半把
        #: 服务打死,这不是设计取舍,是漏了一处"——同一句话对**冷却**也成立,
        #: 而漏的正是这一处:降级路径在正确性上是软的,在延迟上是硬的。
        #:
        #: 实测代价见 redis_health.py 模块 docstring(一句零 LLM 的「你好」12.2 秒)。
        if breaker is None:
            from app.config.settings import settings
            from app.session.redis_health import Breaker
            breaker = Breaker(
                float(getattr(settings, "session_store_redis_retry_cooldown_s", 5.0)),
                name="会话锁", dependency="Redis", clock=now)
        self._breaker = breaker

    @staticmethod
    def _lock_key(key: str) -> str:
        return f"lock:{Path(key).stem}"   # 与 sess:{stem} 对齐

    @contextmanager
    def guard(self, key: str, timeout: Optional[float] = None):
        """抢锁。**Redis 不可用时退到进程内锁,而不是让异常穿透。**

        改造前这里对 Redis 故障零防护:`self._r.set()` 抛 ConnectionError,异常
        一路穿出 SSE 生成器 → 500。实测表现是 Redis 一停,**每一条买家消息都
        收不到任何回复**——不是降级,是整条买家链路当场死掉。

        而紧挨着它的会话存储(`app/session/store.py`)早就实现了优雅降级:连不上
        就回落本地文件,并打一条写明运维影响的 warning。同一次故障里,一半组件
        降级、另一半把服务打死,这不是设计取舍,是漏了一处。

        **为什么退到进程内锁,而不是 fail-open 或 fail-closed:**

        - fail-closed(拿不到锁就拒绝)= 现状的温和版,买家照样得不到回复。
          锁是防串状态的保护措施,不是安全边界,不值得用"谁都别想说话"来换。
        - fail-open(直接放行)= 同一会话可被并发处理,消息顺序与工具副作用
          都可能错乱,而这恰恰是这把锁存在的唯一理由。
        - 进程内锁 = 单实例部署下**完全正确**;多实例下退化为"每个实例内部
          互斥",弱于全局互斥但远好于前两者。这与会话存储降级后的语义
          (本地文件 = 每实例各存各的)是**同一档**,两者对齐才讲得通。

        **降级是有界的,但不再是"只作用于本次调用"。** 这里刻意做了一个取舍:
        失败后开一个冷却窗口(`session_store_redis_retry_cooldown_s`,默认 5 秒),
        窗口内直接走进程内锁、不再碰 Redis。

        为什么要这么换:没有冷却时,一次**持续**故障会让每个买家请求都重连一次、
        再付一次 socket 超时。实测过它的代价——6379 上留着一个已删容器的 Docker
        端口转发,接受 TCP 连接但永不应答,于是一句走规则快路径、零 LLM 调用的
        「你好」要 12.2 秒;加冷却后稳定态是 40 毫秒。

        代价说清楚:冷却期内这把锁只有进程内互斥。但看边际——**故障期间本来就
        没有全局互斥**(两个实例都在回落进程内锁),冷却只是把这个状态从故障结束
        那一刻向后延长最多 cooldown 秒。对全局互斥更敏感的部署把窗口调小即可。

        换不掉的底线仍然成立:**不会永久停在降级态**,冷却到期自动重试,Redis
        恢复即自动切回,不需要重启(见 tests/test_session_lock_degrade.py)。
        """
        rk = self._lock_key(key)
        token = uuid.uuid4().hex
        deadline = self._now() + (self._wait if timeout is None else timeout)
        acquired = False

        # 冷却期内直接走进程内锁,**不再碰 Redis**。少了这一步,一次持续故障
        # 会让每个买家请求都重新连一次、再付一次 socket 超时——功能没坏,但每轮
        # 对话凭空多出秒级延迟,而且日志里看不出这是同一次故障。
        if self._breaker.open:
            with self._fallback.guard(key, timeout=(
                    self._wait if timeout is None else timeout)) as got:
                yield got
            return

        while True:
            try:
                ok = self._r.set(rk, token, nx=True, px=self._ttl_ms)
            except Exception as exc:  # noqa: BLE001 Redis 故障不该打死买家链路
                logger.warning(
                    "会话锁降级:Redis 不可用(%s: %s),本次改用进程内锁。"
                    "【运维须知】降级期间只保证**单实例内**同会话互斥,多实例部署下"
                    "两个实例可能同时处理同一会话(消息顺序/工具副作用有串的风险)。"
                    "与会话存储的降级同源,请尽快恢复 Redis;恢复后下一次调用自动"
                    "走回分布式锁,无需重启。session=%s",
                    type(exc).__name__, exc, key)
                self._breaker.record_failure("acquire", exc)
                with self._fallback.guard(key, timeout=(
                        self._wait if timeout is None else timeout)) as got:
                    yield got
                return
            self._breaker.record_success()
            if ok:
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
        # 强制超时,见 redis_health.py:直接 redis.from_url 不传超时等于无限阻塞。
        from app.session.redis_health import make_client
        return RedisSessionLock(make_client(settings.redis_url),
                                ttl_ms=settings.session_lock_ms)
    return LocalSessionLock()
