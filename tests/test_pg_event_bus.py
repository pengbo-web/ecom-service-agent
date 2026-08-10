"""PG 队列载体(PG2)——**与 SQLite 载体跑同一套语义断言**。

这份测试的形态本身就是要点:每条断言都**参数化跑两套后端**,断言完全一致。
换载体最容易出的错不是"新实现有 bug",而是"新实现在某个细节上和旧的不一样,
而上层依赖了那个细节"——只测新实现是发现不了这类问题的。

没有可用 PG 时整个文件 skip(不静默跳过单条,那会让"PG 路径其实没跑过"看起来
像通过)。启用方式:

    docker run -d --name ecom-pg -p 5442:5432 \
      -e POSTGRES_PASSWORD=ecom_dev_pw -e POSTGRES_USER=ecom -e POSTGRES_DB=ecom \
      postgres:16-alpine
    set ECOM_TEST_PG_DSN=postgresql://ecom:ecom_dev_pw@127.0.0.1:5442/ecom
"""

from __future__ import annotations

import os
import threading

import pytest

PG_DSN = os.getenv("ECOM_TEST_PG_DSN", "").strip()


def _pg_available() -> bool:
    if not PG_DSN:
        return False
    try:
        import psycopg

        with psycopg.connect(PG_DSN, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


pg_only = pytest.mark.skipif(
    not _pg_available(),
    reason="需要可连的 PG:设置 ECOM_TEST_PG_DSN(见本文件头部说明)")


# ---------- 两套后端的夹具 ----------

@pytest.fixture()
def sqlite_bus(tmp_path):
    from app.db import set_db
    from app.db.database import Database
    from app.multi_agent.event_bus import SqliteEventBus

    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    set_db(d)
    yield SqliteEventBus()
    set_db(None)


@pytest.fixture()
def pg_bus():
    if not _pg_available():
        pytest.skip("无可用 PG")
    import psycopg

    from app.multi_agent.pg_event_bus import PostgresEventBus

    bus = PostgresEventBus(PG_DSN)
    bus.init_schema()
    # 每个用例开局清表:队列语义(优先级/FIFO/计数)全都和"表里有什么"相关,
    # 残留行会让断言随执行顺序漂移。
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE agent_events RESTART IDENTITY")
    return bus


@pytest.fixture(params=["sqlite", "pg"])
def bus(request, sqlite_bus):
    """两套后端各跑一遍。PG 不可用时**只跳过 pg 这一档**,sqlite 档照跑。"""
    if request.param == "sqlite":
        return sqlite_bus
    if not _pg_available():
        pytest.skip("无可用 PG")
    return request.getfixturevalue("pg_bus")


# ---------- 语义契约:两套后端必须一致 ----------

def test_publish_then_claim_roundtrip(bus):
    eid = bus.publish("signal.anomaly", {"kind": "x"}, "service", "analyst", "C1")
    assert eid > 0
    got = bus.claim("analyst")
    assert [e["id"] for e in got] == [eid]
    assert got[0]["payload"] == {"kind": "x"}
    assert got[0]["status"] == "processing"


def test_claim_is_scoped_to_target(bus):
    bus.publish("a", {}, "s", "analyst", "C1")
    bus.publish("b", {}, "s", "growth", "C1")
    assert len(bus.claim("analyst")) == 1
    assert len(bus.claim("growth")) == 1


def test_priority_desc_then_fifo(bus):
    """优先级降序,**同优先级严格 FIFO**。

    FIFO 这一点不能丢:同类事件之间的先来后到是可预期性的唯一来源,乱序会让
    "为什么这条比那条晚处理"变得无法解释。PG 的 RETURNING 不保证顺序,所以
    载体层必须自己排回去——这条断言就是钉那个排序。
    """
    low1 = bus.publish("a", {}, "s", "analyst", "C1", priority=0)
    low2 = bus.publish("a", {}, "s", "analyst", "C2", priority=0)
    urgent = bus.publish("a", {}, "s", "analyst", "C3", priority=10)
    ids = [e["id"] for e in bus.claim("analyst")]
    assert ids == [urgent, low1, low2]


def test_claim_respects_limit(bus):
    for _ in range(5):
        bus.publish("a", {}, "s", "analyst", "C1")
    assert len(bus.claim("analyst", limit=2)) == 2


def test_claim_does_not_return_the_same_row_twice(bus):
    """认领过的行不能再被认领——幂等的根基。"""
    bus.publish("a", {}, "s", "analyst", "C1")
    assert len(bus.claim("analyst")) == 1
    assert bus.claim("analyst") == []


@pytest.mark.parametrize("status", ["done", "failed", "skipped"])
def test_finish_three_terminal_states(bus, status):
    eid = bus.publish("a", {}, "s", "analyst", "C1")
    bus.claim("analyst")
    assert bus.finish(eid, status) is True
    # 重复收尾只有第一次生效(条件更新)
    assert bus.finish(eid, status) is False


def test_finish_only_applies_to_processing(bus):
    """没认领过的行不能被终结:否则一条 pending 会被凭空标成 done。"""
    eid = bus.publish("a", {}, "s", "analyst", "C1")
    assert bus.finish(eid, "done") is False


def test_failed_list_and_count(bus):
    eid = bus.publish("a", {"k": 1}, "s", "analyst", "C1")
    bus.claim("analyst")
    bus.finish(eid, "failed")
    assert bus.failed_count() == 1
    rows = bus.failed()
    assert [r["id"] for r in rows] == [eid]


def test_retry_is_idempotent(bus):
    """连点两次、两个运营同时点,只有第一次真的改到。"""
    eid = bus.publish("a", {}, "s", "analyst", "C1")
    bus.claim("analyst")
    bus.finish(eid, "failed")
    assert bus.retry(eid) is True
    assert bus.retry(eid) is False          # 已不在 failed 状态
    assert [e["id"] for e in bus.claim("analyst")] == [eid]   # 真的回到队列了


def test_reclaim_stale_puts_processing_back(bus):
    """救"认领后崩在半路"。阈值 0 让刚认领的也算滞留,便于确定性断言。"""
    eid = bus.publish("a", {}, "s", "analyst", "C1")
    bus.claim("analyst")
    assert bus.reclaim_stale(older_than_seconds=0) >= 1
    assert [e["id"] for e in bus.claim("analyst")] == [eid]


def test_reclaim_clears_consumed_at(bus):
    """`consumed_at` 必须一并清空,否则下一轮 reclaim 会把这条"很久以前认领的"
    pending 行再当滞留处理一次。"""
    bus.publish("a", {}, "s", "analyst", "C1")
    bus.claim("analyst")
    bus.reclaim_stale(older_than_seconds=0)
    row = [e for e in bus.timeline(limit=10)][0]
    assert row["status"] == "pending"
    assert row["consumed_at"] is None


def test_timeline_filters_by_correlation(bus):
    bus.publish("a", {}, "s", "analyst", "CHAIN-1")
    bus.publish("b", {}, "s", "analyst", "CHAIN-2")
    rows = bus.timeline(correlation_id="CHAIN-1")
    assert len(rows) == 1 and rows[0]["correlation_id"] == "CHAIN-1"


def test_timeline_is_newest_first(bus):
    a = bus.publish("a", {}, "s", "analyst", "C1")
    b = bus.publish("b", {}, "s", "analyst", "C1")
    assert [e["id"] for e in bus.timeline()] == [b, a]


def test_timestamps_are_strings(bus):
    """时间戳统一成字符串。

    PG 返回 datetime 对象,而前端与 `list_event_chains` 的调用方都按字符串处理。
    在载体层归一化,不让"换了后端所以时间字段类型变了"漏到上层。
    """
    bus.publish("a", {}, "s", "analyst", "C1")
    row = bus.timeline()[0]
    assert isinstance(row["created_at"], str)


# ---------- 只有 PG 才有的性质 ----------

@pg_only
def test_concurrent_claim_never_double_delivers(pg_bus):
    """**这是迁 PG 的全部理由。**

    SQLite 版是"先 SELECT 候选 id,再逐行带 status='pending' 条件 UPDATE":单实例
    正确,但多实例下两个 worker 会 SELECT 到同一批 id 再互相抢,抢输的 rowcount=0
    白跑一圈。规模越大空转越多,而且**不报错**——只表现为"worker 在跑但吞吐上不去"。

    PG 用 `FOR UPDATE SKIP LOCKED` 让并发 worker 各取各的、互不阻塞。这条断言钉的是
    "总共认领到的行数 == 发布的行数,且无一重复"。
    """
    total = 60
    for i in range(total):
        pg_bus.publish("a", {"i": i}, "s", "analyst", f"C{i}")

    seen: list[int] = []
    lock = threading.Lock()

    def worker():
        while True:
            got = pg_bus.claim("analyst", limit=5)
            if not got:
                return
            with lock:
                seen.extend(e["id"] for e in got)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == total, "认领总数应等于发布数(不丢)"
    assert len(set(seen)) == total, "同一条事件被投递了两次(幂等被破)"


@pg_only
def test_claim_uses_skip_locked(pg_bus):
    """钉住 SQL 里真的有 `FOR UPDATE SKIP LOCKED`。

    并发断言在小数据量下即使没有 SKIP LOCKED 也可能偶然通过(行锁排队而非跳过),
    所以再补一条结构断言——两条一起才说明"并发安全来自 SKIP LOCKED",而不是
    来自运气。
    """
    import inspect

    from app.multi_agent.pg_event_bus import PostgresEventBus

    src = inspect.getsource(PostgresEventBus.claim)
    assert "FOR UPDATE SKIP LOCKED" in src
    assert "priority DESC, id ASC" in src


@pg_only
def test_bad_json_payload_row_is_skipped(pg_bus):
    """坏 JSON 的行跳过,单条脏数据不拖垮整个消费循环(与 SQLite 同口径)。"""
    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO agent_events (event_type, payload, source_agent, "
            "target_agent, correlation_id) VALUES ('a', '{not json', 's', 'analyst', 'C1')")
    good = pg_bus.publish("b", {"ok": 1}, "s", "analyst", "C2")
    ids = [e["id"] for e in pg_bus.claim("analyst")]
    assert ids == [good], "坏行应被跳过,好行照常认领"


# ---------- 工厂降级 ----------

def test_factory_falls_back_to_sqlite_when_pg_unreachable(monkeypatch):
    """配成 pg 但连不上时回落 SQLite 并记 warning,**不抛**。

    队列是买家会话热路径上 `publish` 的落点(客服侧发 service_escalation 旁路
    信号),让一个配置问题把买家链路打死是不成比例的——与会话存储/会话锁的降级
    姿态一致(那两处已经验证过:Redis 一停曾让每条买家消息变成 500)。
    """
    from app.config.settings import settings
    from app.multi_agent import event_bus as eb

    monkeypatch.setattr(settings, "collab_bus_backend", "pg")
    monkeypatch.setattr(settings, "collab_pg_dsn",
                        "postgresql://nobody:nobody@127.0.0.1:1/none")
    eb.set_event_bus(None)
    try:
        got = eb.get_event_bus()
        assert isinstance(got, eb.SqliteEventBus)
    finally:
        eb.set_event_bus(None)


def test_factory_falls_back_when_dsn_missing(monkeypatch):
    from app.config.settings import settings
    from app.multi_agent import event_bus as eb

    monkeypatch.setattr(settings, "collab_bus_backend", "pg")
    monkeypatch.setattr(settings, "collab_pg_dsn", "")
    eb.set_event_bus(None)
    try:
        assert isinstance(eb.get_event_bus(), eb.SqliteEventBus)
    finally:
        eb.set_event_bus(None)


def test_default_backend_is_sqlite(monkeypatch):
    """默认不启用 PG。

    只迁队列有代价:业务数据仍在 SQLite,`publish` 与业务写不在同一事务里。
    这个开关的用途是**验证 PG 路子与多实例认领**,不是"今天就切过去"。
    """
    from app.config.settings import settings

    assert settings.collab_bus_backend == "sqlite"
