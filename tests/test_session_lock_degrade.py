"""会话锁在 Redis 故障下的降级。

**这个缺陷是实跑走查抓到的,不是推演出来的。** 现场表现:Redis 没起,
`/api/chat` 每一条买家消息都返回 500,买家一个字都收不到;而同一次故障里
`app/session/store.py` 正常回落本地文件并打了运维日志。也就是说同一次 Redis
故障中,一半组件优雅降级、另一半把整条买家链路打死。

栈顶是 `app/session/lock.py:guard` 里裸调的 `self._r.set(...)`。
"""

from __future__ import annotations

import threading

import pytest

from app.session.lock import LocalSessionLock, RedisSessionLock


class _DeadRedis:
    """连不上的 Redis:任何命令都抛 ConnectionError(与 redis-py 行为一致)。"""

    def set(self, *a, **kw):
        raise ConnectionError("Error 10061 connecting to 127.0.0.1:6379")

    def pipeline(self, *a, **kw):
        raise ConnectionError("Error 10061 connecting to 127.0.0.1:6379")


class _FlakyRedis:
    """前 n 次连不上,之后恢复正常。用于验证"降级只影响本次调用"。"""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.store: dict = {}
        self.calls = 0

    def set(self, k, v, nx=False, px=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("boom")
        if nx and k in self.store:
            return None
        self.store[k] = v
        return True

    def pipeline(self):
        raise AssertionError("本用例不该走到释放路径")


def test_redis_down_does_not_raise():
    """核心回归:Redis 挂掉时 guard 不能抛异常。

    异常在这里的代价不是"这一轮没锁住",而是穿出 SSE 生成器变成 500——
    买家收不到任何回复。
    """
    lock = RedisSessionLock(_DeadRedis())
    with lock.guard("s1") as got:
        assert got is True, "降级后仍应拿到(进程内)锁,而不是拒绝服务"


def test_degraded_lock_still_excludes_within_process():
    """降级不等于放行:同一进程内仍要互斥。

    fail-open 会让同一会话被并发处理,消息顺序与工具副作用都可能错乱,
    而那正是这把锁存在的唯一理由。
    """
    lock = RedisSessionLock(_DeadRedis())
    entered = threading.Event()
    release = threading.Event()
    second_got = []

    def hold():
        with lock.guard("same"):
            entered.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert entered.wait(timeout=5)

    # 第二个持有者在超时内拿不到锁
    with lock.guard("same", timeout=0.2) as got:
        second_got.append(got)

    release.set()
    t.join(timeout=5)
    assert second_got == [False], "降级期间同会话仍必须互斥"


def test_different_sessions_are_not_blocked_while_degraded():
    """降级不能把不同会话也串起来——那会让一个买家的慢请求拖死其他所有人。"""
    lock = RedisSessionLock(_DeadRedis())
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with lock.guard("a"):
            entered.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert entered.wait(timeout=5)
    with lock.guard("b", timeout=0.2) as got:
        assert got is True
    release.set()
    t.join(timeout=5)


def test_recovery_is_automatic_without_restart():
    """降级只作用于本次调用:Redis 恢复后下一次自动走回分布式锁。

    如果降级写进了实例状态,Redis 恢复后仍会一直用进程内锁,多实例部署下
    会长期处于"看起来正常、实际没有全局互斥"的状态——比直接报错更难发现。
    """
    r = _FlakyRedis(fail_times=1)
    lock = RedisSessionLock(r)

    with lock.guard("s") as got:          # 第 1 次:Redis 挂,走进程内锁
        assert got is True
    assert r.calls == 1

    with lock.guard("s") as got:          # 第 2 次:Redis 好了,应重新尝试
        assert got is True
    assert r.calls == 2, "恢复后必须重新走 Redis,而不是永久停在降级态"
    assert r.store, "第 2 次应真的把锁写进了 Redis"


def test_release_failure_is_still_swallowed():
    """释放路径出错仍靠 TTL 兜底,不能抛。"""
    class _SetOkDelBoom:
        def set(self, *a, **kw):
            return True

        def pipeline(self, *a, **kw):
            raise ConnectionError("down during release")

    lock = RedisSessionLock(_SetOkDelBoom())
    with lock.guard("s") as got:
        assert got is True          # 退出 with 时走 _release,不应抛


def test_local_lock_unchanged():
    """进程内锁本身的行为不变(降级依赖它,不能顺手改坏)。"""
    lock = LocalSessionLock()
    with lock.guard("k") as got:
        assert got is True
        with lock.guard("k", timeout=0.05) as again:
            assert again is False


@pytest.mark.parametrize("exc", [ConnectionError, TimeoutError, OSError])
def test_any_redis_failure_degrades(exc):
    """降级要覆盖各种连接类异常,不能只认 ConnectionError 一种。

    redis-py 在超时/DNS/连接池耗尽等场景抛的异常类型并不统一,按类型白名单
    兜底迟早漏掉一种——而漏掉的那一种就是下一次线上 500。
    """
    class _Boom:
        def set(self, *a, **kw):
            raise exc("boom")

    with RedisSessionLock(_Boom()).guard("s") as got:
        assert got is True
