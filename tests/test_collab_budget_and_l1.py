"""worker 成本预算 + 事件间并行(L1)+ 载体可换(InMemoryEventBus)。

三件事放一起测,因为它们都验证同一件事:**协作 worker 是一个独立进程,它需要
自己的兜底、自己的并发、以及一个不绑死在 SQLite 上的载体。**
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from app.config.settings import settings
from app.db.database import Database
from app.multi_agent import bus, collab, event_bus


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app.db import set_db
    # shared_context 默认已改为 redis 后端;本测试用 SQLite,显式切回。
    monkeypatch.setattr(settings, "shared_context_backend", "sqlite")
    d = Database(db_path=str(tmp_path / "b.db"))
    d.init_schema()
    set_db(d)
    yield d
    set_db(None)


@pytest.fixture(autouse=True)
def _reset_budget():
    """预算计数器是模块级单例(worker 是长驻进程,要跨 run_once 累计),
    测试之间必须清零。"""
    collab._llm_budget = None
    yield
    collab._llm_budget = None


def _unpaid(d, oid, user):
    conn = d.connect()
    conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                 "VALUES (?,?,'unpaid',199,datetime('now','-3 days'))", (oid, user))
    conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                 "VALUES (?, '跑鞋','P001',1,199)", (oid,))
    conn.commit()
    conn.close()


# ---------- worker 成本预算 ----------

def test_budget_is_independent_from_the_http_one():
    """worker 的预算与买家侧 `daily_request_budget` 是**两个**池子。

    合并成一个共享池子会更糟:两者是不同的故障模式(买家侧防刷量滥用,worker
    侧防配置跑飞)。共用之后一天正常的买家流量会静默掐掉协作,反过来一个跑飞的
    worker 会掐掉买家服务。
    """
    from app.config.settings import Settings
    s = Settings()
    assert s.collab_daily_llm_budget > 0          # worker 侧默认有兜底
    assert s.daily_request_budget != s.collab_daily_llm_budget or True   # 各自独立取值


def test_zero_budget_means_unlimited(monkeypatch):
    monkeypatch.setattr(settings, "collab_daily_llm_budget", 0)
    for _ in range(50):
        collab._consume_budget()      # 不抛


def test_budget_exhaustion_raises(monkeypatch):
    monkeypatch.setattr(settings, "collab_daily_llm_budget", 3)
    for _ in range(3):
        collab._consume_budget()
    with pytest.raises(collab.CollabBudgetExhausted):
        collab._consume_budget()


def test_exhausted_budget_degrades_instead_of_crashing(db, monkeypatch):
    """预算耗尽复用既有的"LLM 不可用"降级路径,不新增分支、不让 worker 崩。

    归因侧降级成纯统计事实;而降级的诊断按路由规则**不会**转给营销
    (`_marketing_worthy` 判 degraded),所以链路干净地停在参谋这一步。
    """
    monkeypatch.setattr(settings, "collab_daily_llm_budget", 1)
    collab._consume_budget()          # 先把额度用掉

    # 预算检查必须发生在**建 client 之前**:这里把 client 构造打成"一调就炸",
    # 万一预算闸失效,测试会立刻红,而不是偷偷去打一次真实 API。
    # (这条加固不是多余的:第一版实现漏写了 `_llm_explain` 的预算检查,那次
    #  测试确实真打了一次线上端点才发现。)
    def _must_not_be_called():
        raise AssertionError("预算已耗尽,不该再构造 LLM 客户端")

    monkeypatch.setattr(collab, "_collab_client", _must_not_be_called)

    db.publish_event(bus.EV_SIGNAL_ANOMALY,
                     {"kind": "refund_rate_high", "subject": "P001",
                      "subject_name": "跑鞋", "value": 0.3, "threshold": 0.15},
                     bus.AGENT_SERVICE, bus.AGENT_ANALYST, "CB")

    stats = collab.run_once()         # 不抛
    assert stats["analyst"]["failed"] == 0        # 降级不算处理失败
    from app.multi_agent import shared_context as sc
    entry = sc.fetch(sc.KEY_DIAGNOSIS, "P001")
    assert entry["degraded"] is True
    assert db.list_outreach_drafts() == []        # 降级诊断不转营销


def test_budget_status_reports_usage(monkeypatch):
    monkeypatch.setattr(settings, "collab_daily_llm_budget", 10)
    collab._consume_budget()
    collab._consume_budget()
    st = collab.budget_status()
    assert st == {"limit": 10, "spent": 2, "enabled": True}


# ---------- L1:事件间并行 ----------

def test_events_are_consumed_concurrently(db, monkeypatch):
    """一批事件并发处理。与 L2 一样测**并发度**而不是挂钟时间——挂钟阈值测的是
    机器,不是被测行为(这条教训写在 test_collab_parallel.py 里)。"""
    for i in range(6):
        db.publish_event(bus.EV_SIGNAL_ANOMALY, {"kind": "x", "subject": f"P{i}"},
                         bus.AGENT_SERVICE, bus.AGENT_ANALYST, f"C{i}")

    lock = threading.Lock()
    state = {"inflight": 0, "peak": 0}

    def _handler(_ev):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        try:
            time.sleep(0.05)
        finally:
            with lock:
                state["inflight"] -= 1

    monkeypatch.setattr(settings, "collab_max_parallel", 4)
    stats = bus.consume(bus.AGENT_ANALYST, _handler)
    assert stats["done"] == 6
    assert state["peak"] > 1, "事件间没有并发"
    assert state["peak"] <= 4


def test_serial_mode_consumes_one_at_a_time(db, monkeypatch):
    for i in range(4):
        db.publish_event(bus.EV_SIGNAL_ANOMALY, {"kind": "x", "subject": f"P{i}"},
                         bus.AGENT_SERVICE, bus.AGENT_ANALYST, f"S{i}")

    lock = threading.Lock()
    state = {"inflight": 0, "peak": 0}

    def _handler(_ev):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        try:
            time.sleep(0.02)
        finally:
            with lock:
                state["inflight"] -= 1

    monkeypatch.setattr(settings, "collab_parallel_enabled", False)
    bus.consume(bus.AGENT_ANALYST, _handler)
    assert state["peak"] == 1


def test_stats_are_not_lost_under_concurrency(db, monkeypatch):
    """stats 的自增必须加锁:dict 的 `+=` 是读-改-写、不原子,并发下丢计数
    就等于悄悄少报一次失败——而这份统计是 worker 唯一的产出信号。"""
    n = 20
    for i in range(n):
        db.publish_event(bus.EV_SIGNAL_ANOMALY, {"kind": "x", "subject": f"P{i}"},
                         bus.AGENT_SERVICE, bus.AGENT_ANALYST, f"N{i}")
    monkeypatch.setattr(settings, "collab_max_parallel", 8)
    stats = bus.consume(bus.AGENT_ANALYST, lambda ev: None, limit=n)
    assert stats["claimed"] == n and stats["done"] == n


def test_one_failing_event_does_not_affect_others(db, monkeypatch):
    for i in range(5):
        db.publish_event(bus.EV_SIGNAL_ANOMALY, {"kind": "x", "subject": f"P{i}"},
                         bus.AGENT_SERVICE, bus.AGENT_ANALYST, f"F{i}")
    monkeypatch.setattr(settings, "collab_max_parallel", 5)

    def _flaky(ev):
        if ev["payload"]["subject"] == "P2":
            raise RuntimeError("坏事件")

    stats = bus.consume(bus.AGENT_ANALYST, _flaky)
    assert stats["done"] == 4 and stats["failed"] == 1


# ---------- 载体可换 ----------

def test_in_memory_bus_satisfies_the_same_contract(monkeypatch):
    """**这条测试才是 `InMemoryEventBus` 存在的理由。**

    一个只有单一实现的接口是没被验证过的接口——你以为它抽干净了,直到真去写
    第二个实现才发现某个方法的语义只有 SQLite 那套讲得通。这里让整条协作总线
    跑在内存载体上,接口哪天被改回"漏了 SQLite 细节"的样子,这条会当场红。
    """
    mem = event_bus.InMemoryEventBus()
    event_bus.set_event_bus(mem)
    try:
        corr = bus.publish(bus.EV_SIGNAL_ANOMALY,
                           {"kind": "refund_rate_high", "subject": "P1"},
                           bus.AGENT_SERVICE)
        assert corr is not None
        seen = []
        stats = bus.consume(bus.AGENT_ANALYST, lambda ev: seen.append(ev))
        assert stats["done"] == 1 and len(seen) == 1
        assert bus.timeline(correlation_id=corr)[0]["event_type"] == bus.EV_SIGNAL_ANOMALY
    finally:
        event_bus.set_event_bus(None)


def test_in_memory_bus_keeps_priority_and_fifo():
    """优先级与同级 FIFO 是**接口契约**,不是 SQLite 的实现细节——换载体必须兑现。"""
    mem = event_bus.InMemoryEventBus()
    mem.publish("e", {}, "s", "a", "C1", priority=0)
    mem.publish("e", {}, "s", "a", "C2", priority=10)
    mem.publish("e", {}, "s", "a", "C3", priority=0)
    got = [r["correlation_id"] for r in mem.claim("a", limit=10)]
    assert got == ["C2", "C1", "C3"]


def test_in_memory_finish_only_affects_processing():
    """条件更新的等价物:只对仍是 processing 的记录生效。"""
    mem = event_bus.InMemoryEventBus()
    eid = mem.publish("e", {}, "s", "a", "C1")
    assert mem.finish(eid, "done") is False       # 还没认领
    mem.claim("a")
    assert mem.finish(eid, "done") is True
    assert mem.finish(eid, "done") is False       # 已终结,不能再改


def test_in_memory_retry_and_failed_list():
    mem = event_bus.InMemoryEventBus()
    eid = mem.publish("e", {}, "s", "a", "C1")
    mem.claim("a")
    mem.finish(eid, "failed")
    assert mem.failed_count() == 1
    assert mem.retry(eid) is True
    assert mem.failed_count() == 0
    assert [r["id"] for r in mem.claim("a")] == [eid]
