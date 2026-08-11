"""Redis 故障必须**有界**:强制超时 + 失败后退避冷却。

这组测试钉的是一次实测。本机 6379 上留着一个已删容器的 Docker 端口转发,它
**接受 TCP 连接但永不应答**——比 ConnectionRefused 恶劣得多,后者毫秒级返回。
买家每轮对话的实测代价:

    「你好」(规则快路径,零 LLM 调用)  12.2 秒
    切成 file 会话后端后同一句           2.2 秒   ← 剩下的是 hmdp 身份解析
    四处 Redis 全部绕开                  ~0.2 秒

四个消费方都写了 fail-soft,所以**功能上完全正常**:没报错、没 500、买家照样
收到回复。坏的是延迟——降级路径在正确性上是软的,在延迟上是硬的。两条根因:

  ① `redis.from_url(url)` 不传超时 → redis-py 默认 socket_timeout=None = 无限阻塞;
  ② 只有会话存储做了退避,会话锁与 hmdp 身份解析没有 → 故障期间每个请求重付一次。
"""

import itertools

import pytest

from app.session.redis_health import Breaker, make_client


class FakeClock:
    """可推进的假时钟,不 sleep。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class Boom:
    """连不上的 Redis:每次调用都抛,并记下被调了几次。"""

    def __init__(self):
        self.calls = 0

    def _fail(self, *a, **kw):
        self.calls += 1
        raise ConnectionError("connection accepted but never answered")

    set = _fail
    hget = _fail
    get = _fail


# --------------------------------------------------------------------------
# ① 超时必须被设上:任何路径都不能落到 redis-py 的 None
# --------------------------------------------------------------------------

def test_make_client_always_sets_timeouts():
    c = make_client("redis://127.0.0.1:6379/0")
    kw = c.connection_pool.connection_kwargs
    assert kw.get("socket_connect_timeout") is not None, "连接超时不能是 None(无限阻塞)"
    assert kw.get("socket_timeout") is not None, "读写超时不能是 None(无限阻塞)"
    assert kw["socket_timeout"] <= 5, "热路径上的读写超时不该超过几秒"


def test_make_client_normalizes_localhost():
    """Windows 下 localhost 先解析成 IPv6 ::1 而 Redis 只监听 IPv4 → 连接超时。
    这个归一原先只写在 hmdp_identity 里,另外三处都没有。"""
    c = make_client("redis://localhost:6379/0")
    assert c.connection_pool.connection_kwargs["host"] == "127.0.0.1"


def test_timeouts_come_from_settings(monkeypatch):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "redis_connect_timeout_s", 0.25)
    monkeypatch.setattr(st.settings, "redis_socket_timeout_s", 0.75)
    kw = make_client("redis://127.0.0.1:6379/0").connection_pool.connection_kwargs
    assert kw["socket_connect_timeout"] == 0.25
    assert kw["socket_timeout"] == 0.75


@pytest.mark.parametrize("builder", [
    "app.session.store._build_from_settings",
    "app.session.lock._build_from_settings",
])
def test_real_builders_set_timeouts(builder, monkeypatch):
    """四个消费方都必须经 make_client——这是"每处各写一遍迟早漏一处"的直接教训:
    改造前只有 hmdp_identity 设了超时,store/lock/idempotency 三处都是裸的。"""
    import importlib
    mod_name, fn_name = builder.rsplit(".", 1)
    mod = importlib.import_module(mod_name)

    from app.config import settings as st
    monkeypatch.setattr(st.settings, "session_store_backend", "redis")
    obj = getattr(mod, fn_name)()
    client = getattr(obj, "_r", None)
    assert client is not None, f"{builder} 没有建出 Redis 客户端"
    kw = client.connection_pool.connection_kwargs
    assert kw.get("socket_timeout") is not None, f"{builder} 漏了读写超时"
    assert kw.get("socket_connect_timeout") is not None, f"{builder} 漏了连接超时"


# --------------------------------------------------------------------------
# ② Breaker 本身
# --------------------------------------------------------------------------

def test_breaker_opens_then_recovers():
    clk = FakeClock()
    b = Breaker(5.0, clock=clk)
    assert not b.open
    b.record_failure("op", ConnectionError("x"))
    assert b.open, "失败后应进入冷却"
    clk.advance(4.9)
    assert b.open, "冷却未到期就不该重试"
    clk.advance(0.2)
    assert not b.open, "冷却到期应自动重试"


def test_breaker_success_closes_immediately():
    """恢复不必等窗口自然到期。"""
    clk = FakeClock()
    b = Breaker(60.0, clock=clk)
    b.record_failure("op", ConnectionError("x"))
    assert b.open
    b.record_success()
    assert not b.open


def test_breaker_names_the_right_dependency(caplog):
    """告警里必须点名**真正挂掉的那个依赖**,不能写死成 Redis。

    这条是实跑抓到的:Breaker 被 ApeRAG 检索复用后,日志打出来是
    「ApeRAG 检索 降级:Redis 不可用……请尽快恢复 Redis」——运维照这条去重启
    Redis,而挂的是 ApeRAG。一条把人指错方向的告警比没有告警更糟。
    """
    b = Breaker(5.0, name="ApeRAG 检索", dependency="ApeRAG", clock=FakeClock())
    with caplog.at_level("WARNING"):
        b.record_failure("search", ConnectionError("timed out"))
    msg = caplog.records[-1].getMessage()
    assert "ApeRAG 不可用" in msg
    assert "恢复 ApeRAG" in msg
    assert "Redis" not in msg, "挂的是 ApeRAG,日志里不该出现 Redis"


def test_breaker_dependency_defaults_to_name(caplog):
    """不传 dependency 时跟随 name——会话侧那几处本来就是 Redis,不必重复写一遍。"""
    b = Breaker(5.0, name="会话锁", dependency="Redis", clock=FakeClock())
    with caplog.at_level("WARNING"):
        b.record_failure("acquire", ConnectionError("boom"))
    msg = caplog.records[-1].getMessage()
    assert "会话锁 降级" in msg and "Redis 不可用" in msg


def test_breaker_logs_once_per_outage(caplog):
    """一条淹在几千条重复里的告警等于没有告警:只在"从可用跌到不可用"那一刻警告。"""
    clk = FakeClock()
    b = Breaker(5.0, clock=clk)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            b.record_failure("op", ConnectionError("x"))
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


# --------------------------------------------------------------------------
# ③ 会话锁:冷却期内不再碰 Redis(这一处原先是漏的)
# --------------------------------------------------------------------------

def test_lock_stops_touching_redis_during_cooldown():
    """核心断言:持续故障下,Redis 只被打一次,而不是每个请求一次。

    改造前每个买家请求都会重连一次、再付一次 socket 超时——功能没坏,但每轮
    对话凭空多出秒级延迟。
    """
    from app.session.lock import RedisSessionLock

    clk = FakeClock()
    boom = Boom()
    lk = RedisSessionLock(boom, now=clk, sleep=lambda s: None,
                          breaker=Breaker(5.0, clock=clk))

    for _ in range(4):
        with lk.guard("s-1") as got:
            assert got, "降级到进程内锁后仍必须拿到锁,不能让买家收不到回复"

    assert boom.calls == 1, (
        f"冷却期内不该再连 Redis,实际连了 {boom.calls} 次"
        "——每次都是一整个 socket 超时摞在买家这一轮上")


def test_lock_retries_after_cooldown_expires():
    """不能永久停在降级态:冷却到期要再试一次,Redis 恢复才能自动切回。"""
    from app.session.lock import RedisSessionLock

    clk = FakeClock()
    boom = Boom()
    lk = RedisSessionLock(boom, now=clk, sleep=lambda s: None,
                          breaker=Breaker(5.0, clock=clk))
    with lk.guard("s-1"):
        pass
    assert boom.calls == 1
    clk.advance(6.0)
    with lk.guard("s-1"):
        pass
    assert boom.calls == 2, "冷却到期后应重试,否则 Redis 恢复了也切不回来"


def test_lock_still_works_when_redis_is_healthy():
    """退避不能影响正常路径:健康时每次都要真的去抢分布式锁。"""
    from app.session.lock import RedisSessionLock

    class Ok:
        def __init__(self):
            self.calls = 0

        def set(self, *a, **kw):
            self.calls += 1
            return True

        def watch(self, *a, **kw):
            pass

        def pipeline(self, *a, **kw):
            raise AssertionError("本测试不校验释放路径")

    ok = Ok()
    clk = FakeClock()
    lk = RedisSessionLock(ok, now=clk, sleep=lambda s: None,
                          breaker=Breaker(5.0, clock=clk))
    for _ in range(3):
        try:
            with lk.guard("s-1") as got:
                assert got
        except AssertionError as e:
            if "释放路径" not in str(e):
                raise
    assert ok.calls == 3, "健康时不该跳过 Redis"


# --------------------------------------------------------------------------
# ④ hmdp 身份解析:每个 /api/chat 请求必经的那一步
# --------------------------------------------------------------------------

def test_hmdp_resolve_stops_touching_redis_during_cooldown(monkeypatch):
    """这一处比别处更疼:`resolve_hmdp_user` 在 /api/chat 里每个请求都要走一遍,
    所以 Redis 一挂,每一轮对话固定多付一次超时——实测那条"零 LLM 的固定话术
    也要 2 秒"就是它。"""
    from app.api import hmdp_identity as hi

    clk = FakeClock()
    boom = Boom()
    monkeypatch.setattr(hi, "_redis", boom)
    monkeypatch.setattr(hi, "_breaker", Breaker(5.0, clock=clk))

    for _ in range(4):
        assert hi.resolve_hmdp_user("tok-1") is None
    assert boom.calls == 1, f"冷却期内不该再连 Redis,实际连了 {boom.calls} 次"

    clk.advance(6.0)
    assert hi.resolve_hmdp_user("tok-1") is None
    assert boom.calls == 2, "冷却到期后应重试"


def test_hmdp_resolve_ignores_global_breaker_for_injected_client(monkeypatch):
    """显式传 client 的调用方(测试/内部)用的是另一个连接,凭全局故障判定
    去跳过它们是错的。"""
    from app.api import hmdp_identity as hi

    clk = FakeClock()
    b = Breaker(5.0, clock=clk)
    b.record_failure("x", ConnectionError("x"))
    monkeypatch.setattr(hi, "_breaker", b)

    class Ok:
        def hget(self, *a, **kw):
            return b"1011"

    assert hi.resolve_hmdp_user("tok-1", redis_client=Ok()) == "1011"


def test_hmdp_resolve_success_closes_cooldown(monkeypatch):
    from app.api import hmdp_identity as hi

    clk = FakeClock()
    br = Breaker(60.0, clock=clk)
    monkeypatch.setattr(hi, "_breaker", br)

    calls = itertools.count()

    class Flaky:
        def hget(self, *a, **kw):
            if next(calls) == 0:
                raise ConnectionError("down")
            return b"1011"

    monkeypatch.setattr(hi, "_redis", Flaky())
    assert hi.resolve_hmdp_user("tok") is None
    assert br.open
    clk.advance(61.0)
    assert hi.resolve_hmdp_user("tok") == "1011"
    assert not br.open, "成功一次就该立刻关掉冷却,不必等窗口到期"
