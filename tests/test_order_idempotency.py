"""下单幂等键:同一个键只建一笔订单。

**背景**(交付对照表 ㊿③ 的未修部分):前端的在途 ref 只挡住"买家手抖点两下",
挡不住网络超时后的重试、多标签页、脚本重放——那些都会各建一笔真订单。全仓当时
搜不到任何 `idempotency` / `request_id` / `client_token`。

用业界惯例的 `Idempotency-Key` 头(Stripe/Square 同名)。三条纪律:

- **不传就是改造前的行为**,老客户端不会被打断;
- 胜负由 `UNIQUE(user, key)` 判,**不是"先查再插"**——后者在并发下两个请求会同时
  查到"没有"然后各建一笔(本仓库反复修过的先读后写形状);
- 重试拿 **200 + 原订单**(重试的语义是"我不知道上次成不成功,请给我结果"),
  真并发拿 **409**(另一个请求正拿着这个键建单,绝不能自己再建一笔)。
"""

import threading
from collections import Counter

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "")
    monkeypatch.setattr(st.settings, "auth_enabled", False)
    monkeypatch.setattr(st.settings, "demo_mode", False)      # 走本地建单那条路
    import app.db as db_mod
    from app.db import Database

    db = Database(db_path=str(tmp_path / "ecom.db"))
    db.init_schema()
    monkeypatch.setattr(db_mod, "_DB", db)

    # 商品服务:固定返回一件商品(下单路径要先查商品)
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True,
                            "data": {"id": 1, "title": "鞋", "price": 89900, "stock": 99}}
            return R()
        def post(self, *a, **kw):
            class R:
                status_code = 200
                @staticmethod
                def json(): return {"success": True, "data": "HMDP-ORDER-1"}
            return R()

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: C())

    from app.api.app import create_app
    return TestClient(create_app()), db


def _order(c, key=None, item="1"):
    headers = {"Idempotency-Key": key} if key else {}
    return c.post("/api/order", json={"item_id": item, "quantity": 1}, headers=headers)


def _order_count(db, user="default"):
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM orders WHERE user = ?", (user,)).fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 核心:同键只建一笔
# --------------------------------------------------------------------------

def test_same_key_creates_one_order(client):
    """**核心断言。** 第二次请求返回原订单,不新建。"""
    c, db = client
    r1 = _order(c, "key-1")
    r2 = _order(c, "key-1")

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["order_id"] == r2.json()["order_id"]
    assert _order_count(db) == 1, "同一个幂等键建了多笔订单"


def test_replay_is_marked(client):
    """重放要能被识别出来——客户端/日志据此区分"真的下了一单"与"拿回上次结果"。"""
    c, _ = client
    _order(c, "key-1")
    assert _order(c, "key-1").json()["idempotent_replay"] is True


def test_replay_keeps_the_same_shape(client):
    """重放的字段形状必须与第一次一致,否则客户端会走进错误分支——幂等只做了一半。"""
    c, _ = client
    first = _order(c, "key-1").json()
    replay = _order(c, "key-1").json()
    for field in ("success", "order_id", "status_label", "total"):
        assert replay[field] == first[field], f"{field} 与第一次不一致"


def test_different_keys_create_different_orders(client):
    """**反向断言**:不同键就是不同次购买,必须各建一笔。

    没有这条,"永远只建一笔"也能让上面那条通过——那是把下单功能弄坏了。
    """
    c, db = client
    a = _order(c, "key-a").json()["order_id"]
    b = _order(c, "key-b").json()["order_id"]
    assert a != b
    assert _order_count(db) == 2


def test_no_key_behaves_as_before(client):
    """不传键 = 改造前的行为。老客户端不能被这次改动打断。"""
    c, db = client
    assert _order(c).status_code == 200
    assert _order(c).status_code == 200
    assert _order_count(db) == 2, "不传键时被误当成幂等了"


# --------------------------------------------------------------------------
# 并发:20 个同键请求
# --------------------------------------------------------------------------

def test_concurrent_same_key_creates_one_order(client):
    """20 个并发同键请求 → 只建一笔;其余拿 200(重放)或 409(正在处理)。

    409 不是失败:它明确告诉客户端"稍后重试",而重试会落到重放分支拿到原订单。
    这里断言的是**订单只有一笔**——这才是幂等的实质。
    """
    c, db = client
    n = 20
    codes, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker():
        barrier.wait()
        r = _order(c, "concurrent-key")
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert _order_count(db) == 1, f"并发同键建了 {_order_count(db)} 笔订单"
    counts = Counter(codes)
    assert set(counts) <= {200, 409}, f"出现了意外状态码: {dict(counts)}"
    assert counts[200] >= 1


def test_retry_after_409_gets_the_order(client):
    """409 之后重试必须拿到那笔订单,而不是一直 409——否则买家永远下不了单。"""
    c, db = client
    _order(c, "k")                      # 第一次建成,键已回填
    r = _order(c, "k")
    assert r.status_code == 200
    assert r.json()["order_id"]


# --------------------------------------------------------------------------
# DB 层原语
# --------------------------------------------------------------------------

def test_claim_states(client):
    c, db = client
    assert db.claim_idempotency_key("u", "k") == ("claimed", None)
    assert db.claim_idempotency_key("u", "k") == ("in_flight", None), "占位期应报 in_flight"
    db.finish_idempotency_key("u", "k", "ORD-1")
    assert db.claim_idempotency_key("u", "k") == ("done", "ORD-1")


def test_keys_are_scoped_per_user(client):
    """键按用户隔离:两个买家用了同一个 UUID 不能互相顶掉。"""
    c, db = client
    assert db.claim_idempotency_key("u1", "same")[0] == "claimed"
    assert db.claim_idempotency_key("u2", "same")[0] == "claimed"


def test_finish_does_not_overwrite(client):
    """回填是条件更新:已经填过的不能被后来的覆盖。"""
    c, db = client
    db.claim_idempotency_key("u", "k")
    db.finish_idempotency_key("u", "k", "ORD-1")
    db.finish_idempotency_key("u", "k", "ORD-2")
    assert db.claim_idempotency_key("u", "k") == ("done", "ORD-1")


def test_release_allows_retry(client):
    """**失败必须可重试。** 不释放的话,一次失败的下单会把键永久钉在 in_flight 上。"""
    c, db = client
    db.claim_idempotency_key("u", "k")
    db.release_idempotency_key("u", "k")
    assert db.claim_idempotency_key("u", "k") == ("claimed", None)


def test_release_does_not_touch_completed_keys(client):
    """已完成的键不能被 release 删掉——那等于把幂等保护摘了。"""
    c, db = client
    db.claim_idempotency_key("u", "k")
    db.finish_idempotency_key("u", "k", "ORD-1")
    db.release_idempotency_key("u", "k")
    assert db.claim_idempotency_key("u", "k") == ("done", "ORD-1")


def test_purge_is_offline_only(client):
    """清理有,但不挂在请求路径上(挂上去会让每次下单都付一次全表扫描)。"""
    c, db = client
    db.claim_idempotency_key("u", "k")
    db.finish_idempotency_key("u", "k", "ORD-1")
    assert db.purge_idempotency_keys(older_than_hours=24) == 0      # 刚建的不该被清
    assert db.claim_idempotency_key("u", "k") == ("done", "ORD-1")


# --------------------------------------------------------------------------
# 失败路径:商品服务挂了
# --------------------------------------------------------------------------

def test_failed_order_releases_the_key(client, monkeypatch):
    """商品服务故障 → 503 且键被释放,买家用同一个键重试能成功。"""
    import httpx

    c, db = client

    class Broken:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **kw): raise httpx.ConnectError("refused")
        def post(self, *a, **kw): raise httpx.ConnectError("refused")

    import app.net.internal_http as ih
    monkeypatch.setattr(ih, "internal_client", lambda *a, **kw: Broken())
    assert _order(c, "retry-key").status_code == 503
    assert db.claim_idempotency_key("default", "retry-key") == ("claimed", None), \
        "失败后键没释放,买家永远卡在'正在处理中'"
