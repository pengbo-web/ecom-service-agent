"""R1:SessionStore 抽象(File / Redis 可插拔)。Redis 用 fakeredis,不触网。"""

import fakeredis
import pytest

from app.session.store import (
    FileSessionStore, RedisSessionStore,
    get_session_store, set_session_store,
)
from app.config.settings import settings


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
