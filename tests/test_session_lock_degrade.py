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
from app.session.redis_health import Breaker


class _FakeClock:
    """可推进的假时钟:验证冷却窗口不必真的 sleep。"""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


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
    """降级**有界**:Redis 恢复后最多一个冷却窗口就自动走回分布式锁。

    如果降级写进了实例状态且**永不**复位,Redis 恢复后会一直用进程内锁,多实例
    部署下长期处于"看起来正常、实际没有全局互斥"的状态——比直接报错更难发现。
    这条断言就是为了挡住那种永久 latch。

    **这个测试原来断言的是"下一次调用立刻重试",现在改成"最多一个冷却窗口"。**
    这不是把断言放宽去迁就实现,是一个明确的取舍,理由在实测数据里:

      6379 上留着一个已删容器的 Docker 端口转发,接受 TCP 连接但永不应答。
      没有冷却时,故障期间**每个买家请求**都要重连一次、再付一次 socket 超时;
      一句走规则快路径、零 LLM 调用的「你好」因此要 12.2 秒。加冷却后同一句
      在稳定态是 40 毫秒。

    代价说清楚:冷却期内这把锁确实只有进程内互斥。但要看边际——**故障期间本来
    就没有全局互斥**(两个实例都在回落进程内锁),冷却只是把这个状态从故障结束
    那一刻**向后延长最多 cooldown 秒**。用"恢复后最多 5 秒仍是弱互斥"换"故障期间
    每轮对话不再多付一次超时",在一个在线客服里是划得来的;而且窗口长度是配置项
    (`session_store_redis_retry_cooldown_s`),对全局互斥更敏感的部署可以调小。

    换不掉的是那条底线:**不能永久停在降级态**——所以下面仍然验证它会自己回来。
    """
    clk = _FakeClock()
    r = _FlakyRedis(fail_times=1)
    lock = RedisSessionLock(r, now=clk, sleep=lambda s: None,
                            breaker=Breaker(5.0, clock=clk))

    with lock.guard("s") as got:          # 第 1 次:Redis 挂,走进程内锁
        assert got is True
    assert r.calls == 1

    with lock.guard("s") as got:          # 冷却期内:不该再碰 Redis
        assert got is True
    assert r.calls == 1, "冷却期内每个请求都重连一次,等于把 socket 超时摞在买家身上"

    clk.advance(5.1)
    with lock.guard("s") as got:          # 冷却到期:Redis 好了,自动走回
        assert got is True
    assert r.calls == 2, "冷却到期后必须重新走 Redis,而不是永久停在降级态"
    assert r.store, "这一次应真的把锁写进了 Redis"


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
