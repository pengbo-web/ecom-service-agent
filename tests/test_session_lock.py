"""R4:会话级并发锁(Local 进程内 / Redis 分布式)+ /api/chat 忙时提示。"""

import fakeredis
from fastapi.testclient import TestClient

from app.session.lock import LocalSessionLock, RedisSessionLock, set_session_lock
from app.api.app import create_app
from app.api.session_manager import SessionManager


# ---- LocalSessionLock ----
def test_local_lock_blocks_when_held_then_frees():
    L = LocalSessionLock()
    inner = L._lock_for("s")
    inner.acquire()                       # 模拟另一持有者
    try:
        with L.guard("s", timeout=0.1) as got:
            assert got is False           # 抢不到
    finally:
        inner.release()
    with L.guard("s", timeout=0.1) as got:
        assert got is True                # 释放后可得


# ---- RedisSessionLock(fakeredis)----
def test_redis_lock_acquire_and_release():
    r = fakeredis.FakeStrictRedis()
    L = RedisSessionLock(r, ttl_ms=5000)
    with L.guard("app/sessions/api/s1.json") as got:
        assert got is True
        assert r.exists("lock:s1")        # 持有期间锁存在(key = lock:{stem})
    assert not r.exists("lock:s1")        # 退出即释放


def test_redis_lock_blocks_when_held_and_no_wrong_delete():
    r = fakeredis.FakeStrictRedis()
    r.set("lock:s1", "other-token")       # 别的持有者(无过期)
    L = RedisSessionLock(r, wait_timeout=0.2, retry_interval=0.05)
    with L.guard("app/sessions/api/s1.json") as got:
        assert got is False               # 抢不到
    assert r.get("lock:s1") == b"other-token"   # Lua 校验 token,未误删别人的锁


def test_redis_lock_tokens_unique_between_acquisitions():
    r = fakeredis.FakeStrictRedis()
    L = RedisSessionLock(r, ttl_ms=5000)
    with L.guard("s.json"):
        pass
    with L.guard("s.json") as got:        # 上一次已释放 → 能再获得
        assert got is True


# ---- /api/chat 忙时返回"处理中" ----
class FakeAgent:
    def __init__(self, p):
        self.session_path = p
        self.raw_messages = []
    def save(self):
        pass


def test_chat_returns_busy_when_session_locked():
    from app.db import get_db
    r = fakeredis.FakeStrictRedis()
    set_session_lock(RedisSessionLock(r, wait_timeout=0.15, retry_interval=0.05))
    mgr = SessionManager(agent_factory=lambda p, u=None: FakeAgent(p))
    client = TestClient(create_app(session_manager=mgr))
    # 先开一个"属于该用户且 open"的真实会话,否则 ensure_active 会把未知 ID 换发成新会话,
    # 锁键随之改变,测不到忙路径(auth 关闭时 chat 的 user 回退为 "default")。
    cid = get_db().create_conversation("default")["conversation_id"]
    r.set(f"lock:{cid}", "held-by-another-instance")   # 该会话已被别人占用
    resp = client.post("/api/chat", json={"session_id": cid, "message": "查一下订单 O1"})
    assert resp.status_code == 200
    assert "处理中" in resp.text          # 抢不到锁 → 提示稍候,不并发处理
