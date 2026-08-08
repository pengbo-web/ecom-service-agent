"""R1:SessionStore 抽象(File / Redis 可插拔)。Redis 用 fakeredis,不触网。

R1.x:Redis 连接失败/超时时的容灾降级(回退 FileSessionStore + 退避冷却)。
用一个可控调用计数、可配置失败次数的假客户端模拟"连不上"，不触网、不用真
sleep(时钟可注入)。
"""

import logging

import fakeredis
import pytest
import redis.exceptions

from app.session.store import (
    FileSessionStore, RedisSessionStore,
    get_session_store, set_session_store,
)
from app.config.settings import settings


class _FakeClock:
    """可手动推进的假时钟,替代 time.monotonic,测试无需真的 sleep。"""

    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FlakyClient:
    """模拟"连不上 Redis"的假客户端:前 fail_times 次调用抛连接异常,之后正常
    读写(用普通 dict 存,不触网)。calls 记总调用次数,用于验证退避是否生效
    (冷却期内是否还在反复尝试连接)。"""

    def __init__(self, fail_times: int = 0):
        self.calls = 0
        self.fail_times = fail_times
        self._data: dict = {}

    def _maybe_fail(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise redis.exceptions.ConnectionError(
                "Error 10061 connecting to 127.0.0.1:6379"
            )

    def get(self, key):
        self._maybe_fail()
        return self._data.get(key)

    def set(self, key, value, ex=None):
        self._maybe_fail()
        self._data[key] = value

    def delete(self, key):
        self._maybe_fail()
        self._data.pop(key, None)


@pytest.fixture(autouse=True)
def _reset_store():
    yield
    set_session_store(None)   # 每个测试后复位全局 store,不污染其它测试


def _state(msg="hi"):
    return {"version": 1, "messages": [{"role": "user", "content": msg}],
            "summary": None, "short_term_memory": None}


# ---- FileSessionStore ----
def test_file_store_roundtrip(tmp_path):
    s = FileSessionStore()
    p = str(tmp_path / "sess-1.json")
    assert s.load(p) is None
    s.save(p, _state("hi"))
    assert s.load(p)["messages"][0]["content"] == "hi"
    s.delete(p)
    assert s.load(p) is None


# ---- RedisSessionStore(fakeredis)----
def test_redis_store_roundtrip():
    r = fakeredis.FakeStrictRedis()
    s = RedisSessionStore(r, ttl=100)
    key = "app/sessions/api/alice--web1.json"
    assert s.load(key) is None
    st = _state("你好"); st["status"] = "complete"
    s.save(key, st)
    got = s.load(key)
    assert got["messages"][0]["content"] == "你好" and got["status"] == "complete"
    assert r.exists("sess:alice--web1")          # 键 = sess:{stem}
    assert 0 < r.ttl("sess:alice--web1") <= 100   # TTL 已设
    s.delete(key)
    assert s.load(key) is None


def test_redis_key_uses_path_stem():
    r = fakeredis.FakeStrictRedis()
    s = RedisSessionStore(r)
    s.save("/any/dir/bob--x9.json", _state())
    assert r.exists("sess:bob--x9")


# ---- 工厂 / 注入 ----
def test_factory_defaults_to_file(monkeypatch):
    monkeypatch.setattr(settings, "session_store_backend", "file")
    set_session_store(None)
    assert isinstance(get_session_store(), FileSessionStore)


def test_set_session_store_injection():
    fake = RedisSessionStore(fakeredis.FakeStrictRedis())
    set_session_store(fake)
    assert get_session_store() is fake


# ---- R1.x:Redis 不可达时的容灾降级 ----

def test_load_and_save_survive_redis_down(tmp_path):
    """Redis 连不上时,load/save 都不抛异常,数据经文件路径落地/取回。"""
    key = str(tmp_path / "sess-degrade.json")
    client = _FlakyClient(fail_times=100)   # 一直连不上
    fallback = FileSessionStore()
    s = RedisSessionStore(client, fallback=fallback, retry_cooldown_s=5.0,
                           clock=_FakeClock())

    assert s.load(key) is None                 # 不抛异常
    s.save(key, _state("降级也要能聊"))          # 不抛异常
    got = s.load(key)
    assert got is not None and got["messages"][0]["content"] == "降级也要能聊"
    # 直接读文件后端确认数据确实落在文件路径,不是靠内存幻觉
    assert fallback.load(key)["messages"][0]["content"] == "降级也要能聊"


def test_degradation_emits_warning_and_event(caplog, tmp_path):
    """降级必须大声:日志 warning + 可被日志管道当结构化事件抓取的 extra 字段,
    且警告文本得说清楚"多实例不再共享会话状态"这个真实代价。"""
    key = str(tmp_path / "sess-warn.json")
    client = _FlakyClient(fail_times=100)
    s = RedisSessionStore(client, retry_cooldown_s=5.0, clock=_FakeClock())

    with caplog.at_level(logging.WARNING, logger="app.session.store"):
        s.load(key)

    records = [r for r in caplog.records if r.name == "app.session.store"]
    assert records, "应有一条 warning 日志"
    rec = records[0]
    assert "Redis" in rec.message and "降级" in rec.message
    assert "多实例部署" in rec.message and "不再跨实例共享" in rec.message
    assert getattr(rec, "event", None) == "session_store_degraded"
    assert getattr(rec, "backend", None) == "redis"
    assert getattr(rec, "op", None) == "load"


def test_backoff_avoids_reconnect_attempt_while_cooling_down(tmp_path):
    """冷却期内多次调用,底层客户端只应被真正尝试连接一次——不能让一个挂掉的
    Redis 在冷却期内每次调用都重新承担一次连接超时。"""
    key = str(tmp_path / "sess-backoff.json")
    client = _FlakyClient(fail_times=100)
    clock = _FakeClock()
    s = RedisSessionStore(client, retry_cooldown_s=5.0, clock=clock)

    s.load(key)                       # 第 1 次:真的尝试连接,失败 → 记冷却
    assert client.calls == 1
    s.save(key, _state("x"))          # 冷却期内:应直接跳过 redis
    s.load(key)                       # 冷却期内:应直接跳过 redis
    s.delete(key)                     # 冷却期内:应直接跳过 redis
    assert client.calls == 1, "冷却期内不应再对 Redis 发起连接尝试"


def test_redis_used_again_after_cooldown_no_permanent_latch(tmp_path):
    """Redis 恢复后应自动切回——不能永久停留在降级态。用两份不同内容区分
    "这次读到的到底是文件兜底的旧数据,还是 Redis 恢复后的新数据"。"""
    key = str(tmp_path / "sess-recover.json")
    client = _FlakyClient(fail_times=1)     # 只失败一次,之后正常
    fallback = FileSessionStore()
    clock = _FakeClock()
    s = RedisSessionStore(client, fallback=fallback, retry_cooldown_s=5.0, clock=clock)

    s.save(key, _state("走文件兜底"))          # 第1次调用失败 → 落文件
    assert client.calls == 1
    assert fallback.load(key)["messages"][0]["content"] == "走文件兜底"

    clock.advance(5.1)                       # 冷却期已过
    s.save(key, _state("Redis已恢复"))         # 应重新尝试 Redis,这次成功
    assert client.calls == 2
    got = s.load(key)                        # 再读一次:应来自 Redis,不是文件里那份旧数据
    assert client.calls == 3
    assert got["messages"][0]["content"] == "Redis已恢复"
    # 文件里仍是旧的降级期数据,证明这次读的确不是文件兜底
    assert fallback.load(key)["messages"][0]["content"] == "走文件兜底"


def test_corrupt_payload_still_returns_none_not_treated_as_degrade(tmp_path, caplog):
    """连接问题之外的数据错误(损坏 JSON)必须保持原行为:返回 None,
    不触发降级(冷却窗口不应被打开),也不该被回退文件路径悄悄掩盖。"""
    key = str(tmp_path / "sess-corrupt.json")
    r = fakeredis.FakeStrictRedis()
    clock = _FakeClock()
    s = RedisSessionStore(r, retry_cooldown_s=5.0, clock=clock)
    r.set(s._redis_key(key), "not-json-at-all")   # 直连底层写入非法负载

    with caplog.at_level(logging.WARNING, logger="app.session.store"):
        result = s.load(key)

    assert result is None
    assert not any("降级" in rec.message for rec in caplog.records
                   if rec.name == "app.session.store"), "数据错误不应被记成降级事件"
    assert s._redis_available() is True, "损坏 payload 不应打开退避冷却窗口"


# ---- EcomAgent 经 store 持久化(端到端往返,不调 LLM)----
def test_ecomagent_persists_and_restores_via_store(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    set_session_store(RedisSessionStore(fakeredis.FakeStrictRedis()))
    from app.agent.chat import EcomAgent

    a = EcomAgent(session_path=str(tmp_path / "s.json"), session_id="s", user_id="u")
    a.raw_messages = [{"role": "user", "content": "记住我"}]
    a.save()

    b = EcomAgent(session_path=str(tmp_path / "s.json"), session_id="s", user_id="u")
    assert b.raw_messages == [{"role": "user", "content": "记住我"}]
