"""R6:写工具幂等——重试/重放同一写操作不重复副作用(返回上次成功结果)。"""

import json

import fakeredis
import pytest

from app.db.database import Database
from app.db import set_db
from app.session.idempotency import (
    idempotency_key, RedisIdempotencyStore, NullIdempotencyStore, set_idempotency_store,
)
from app.agent.consent import consent_scope
from app.agent.tools.bargain import set_current_session
from app.agent.tools.registry import execute_tool


# ---- 幂等键 ----
def test_key_stable_and_arg_sensitive():
    k1 = idempotency_key("s", "cancel_order", {"order_id": "O1"})
    k2 = idempotency_key("s", "cancel_order", {"order_id": "O1"})
    k3 = idempotency_key("s", "cancel_order", {"order_id": "O2"})
    assert k1 == k2 and k1 != k3
    assert k1.startswith("applied:")


# ---- Redis 存储 ----
def test_redis_idem_store_roundtrip():
    s = RedisIdempotencyStore(fakeredis.FakeStrictRedis(), ttl=100)
    assert s.get("applied:x") is None
    s.put("applied:x", '{"success": true}')
    assert s.get("applied:x") == '{"success": true}'


# ---- execute_tool 幂等去重 ----
@pytest.fixture
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    conn = d.connect()
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) VALUES (?,?,?,?,?)",
                 ("ORD-20240101-001", "u", "pending", 100.0, "2024-01-01"))
    conn.commit(); conn.close()
    set_db(d)
    return d


def test_cancel_twice_dedup_returns_cached_success(db):
    set_idempotency_store(RedisIdempotencyStore(fakeredis.FakeStrictRedis()))
    set_current_session("sess-A")
    with consent_scope({"cancel_order"}):
        r1 = json.loads(execute_tool("cancel_order", {"order_id": "ORD-20240101-001"}))
        r2 = json.loads(execute_tool("cancel_order", {"order_id": "ORD-20240101-001"}))
    assert r1["success"] is True
    assert r2 == r1                     # 第二次命中幂等缓存 → 同一成功结果
    assert "已取消" not in r2.get("error", "")   # 不是"已取消"报错(没重复执行到 guard)


def test_without_idempotency_second_cancel_errors(db):
    set_idempotency_store(NullIdempotencyStore())   # 不去重
    set_current_session("sess-B")
    with consent_scope({"cancel_order"}):
        r1 = json.loads(execute_tool("cancel_order", {"order_id": "ORD-20240101-001"}))
        r2 = json.loads(execute_tool("cancel_order", {"order_id": "ORD-20240101-001"}))
    assert r1["success"] is True
    assert r2.get("success") is False   # 无幂等:第二次真跑到 guard,返回"已取消"错误


def test_need_confirm_not_cached(db):
    # 未授权 → need_confirm(未执行)不应被缓存;之后授权仍能执行
    store = RedisIdempotencyStore(fakeredis.FakeStrictRedis())
    set_idempotency_store(store)
    set_current_session("sess-C")
    r_nc = json.loads(execute_tool("cancel_order", {"order_id": "ORD-20240101-001"}))   # 无 consent
    assert r_nc.get("need_confirm") is True
    with consent_scope({"cancel_order"}):
        r_ok = json.loads(execute_tool("cancel_order", {"order_id": "ORD-20240101-001"}))
    assert r_ok["success"] is True      # need_confirm 没被缓存,授权后正常执行
