"""事件总线的**载体**抽象:把"事件存在哪、怎么取"与"谁订阅、什么优先级、
什么时候拦"分开。

分工:
  `app/multi_agent/routing.py`  —— 编排语义(订阅表、优先级、消费闸)
  `app/multi_agent/bus.py`      —— 对外门面(publish / consume / 时间线 / 重试)
  本模块                        —— 载体实现(当前只有 SQLite)

为什么要抽这一层:换消息中间件时,改动面必须是**可指认的一个类**,而不是散在
各处的 `get_db().xxx_events(...)`。改造前 `bus.py` 只封了 publish/consume,而
协作时间线、失败列表、手动重试、滞留回收这四条路径**绕过门面直接操作事件表**
——也就是说"换载体只改一个文件"这句话对三个 Agent 是真的,对观测与管理路径
是假的。本模块把那 5 处收进来。

**这不是为了明天就换 Kafka。** 当前负载(单店、单实例、worker 60s 轮询、一次
扫描几十条事件)完全不需要消息中间件,引入它是纯运维负债。抽这层的价值在于:
架构边界变得可指认——能指着 `EventBus` 说"换载体的改动面就是这一个实现类",
而不是指着一个跑着 Kafka 的单机系统解释为什么要用它。

真要换的那天,已知的四处**语义鸿沟**必须先解决(Kafka 一个都不提供):
  1. 优先级认领 —— Kafka 分区内严格按 offset,没有优先级队列
  2. 单条 skipped/failed 状态 —— offset 只能前进,要靠重试 topic + 死信 topic
  3. 按 correlation_id 查询时间线 —— Kafka 不是数据库,查不了
  4. 按 id 精确重投 —— DLQ 里也做不到
所以那天的正确形态是**叠加而非替换**:Kafka 走传输,本表继续作查询投影
(而 `agent_events` 已经天然是一个 Transactional Outbox,最难的那一半已经做完)。
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

from app.db import get_db

logger = logging.getLogger(__name__)


class EventBus(Protocol):
    """事件载体接口。实现者只需保证语义,不规定存储形态。

    每个方法的语义契约(换载体时必须逐条兑现,否则上层会静默出错):

    - `publish`:落一条投递记录,初始状态 pending。**不做路由**——投给谁、
      什么优先级由 `routing` 算好后传进来。
    - `claim`:原子认领该 agent 的 pending 记录并置 processing,**同一条记录
      在并发下只能被一个调用者拿到**(这是幂等的根基)。按 priority 降序、
      同优先级 FIFO。
    - `finish`:把 processing 记录终结为 done/failed/skipped,**只对仍是
      processing 的记录生效**(条件更新),返回是否真的改到。
    - `reclaim_stale`:把滞留在 processing 超过阈值的记录放回 pending
      (救"认领后崩在半路")。
    - `timeline` / `failed` / `failed_count` / `retry`:观测与人工干预出口。
    """

    def publish(self, event_type: str, payload: dict, source: str, target: str,
                correlation_id: str, priority: int = 0,
                status: str = "pending") -> int: ...

    def claim(self, target: str, limit: int = 20) -> list[dict]: ...

    def finish(self, event_id: int, status: str) -> bool: ...

    def reclaim_stale(self, older_than_seconds: int = 300) -> int: ...

    def timeline(self, correlation_id: Optional[str] = None,
                 limit: int = 100) -> list[dict]: ...

    def failed(self, limit: int = 50) -> list[dict]: ...

    def failed_count(self) -> int: ...

    def retry(self, event_id: int) -> bool: ...


class SqliteEventBus:
    """SQLite 载体:每条投递记录是 `agent_events` 表里的一行。

    这个实现是**薄的**——所有语义都已经在 `Database` 的方法里实现好了(条件
    更新的幂等纪律、优先级排序、滞留回收),这里只是把它们收拢到一个可替换的
    对象后面。刻意不在这里加逻辑:载体层多一分自己的判断,换载体时就多一分
    要重新验证的东西。

    不缓存 `get_db()` 的返回:测试与脚本会用 `set_db()` 换库(见
    tests/test_growth_api.py 的夹具),缓存住会让换库不生效。每次调用现取,
    开销就是一次全局变量读。
    """

    def publish(self, event_type: str, payload: dict, source: str, target: str,
                correlation_id: str, priority: int = 0,
                status: str = "pending") -> int:
        return get_db().publish_event(event_type, payload, source, target,
                                      correlation_id, priority=priority,
                                      status=status)

    def claim(self, target: str, limit: int = 20) -> list[dict]:
        return get_db().claim_events(target, limit=limit)

    def finish(self, event_id: int, status: str) -> bool:
        return get_db().finish_event(event_id, status)

    def reclaim_stale(self, older_than_seconds: int = 300) -> int:
        return get_db().reclaim_stale_events(older_than_seconds=older_than_seconds)

    def timeline(self, correlation_id: Optional[str] = None,
                 limit: int = 100) -> list[dict]:
        return get_db().list_events(correlation_id=correlation_id, limit=limit)

    def failed(self, limit: int = 50) -> list[dict]:
        return get_db().list_failed_events(limit=limit)

    def failed_count(self) -> int:
        return get_db().count_failed_events()

    def retry(self, event_id: int) -> bool:
        return get_db().retry_failed_event(event_id)


_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """当前载体。与 `get_session_store()` 同一惯例(惰性单例 + 可注入)。

    按 `collab_bus_backend` 选载体。**配置成 pg 却建不起来时回落 SQLite 并记
    warning,不抛**:队列是买家会话热路径上 `publish` 的落点(客服侧发
    `service_escalation` 旁路信号),让一个配置问题把买家链路打死是不成比例的。
    与会话存储/会话锁的降级姿态一致——那两处已经验证过这条取舍(见
    app/session/lock.py 的注释:Redis 一停曾让每条买家消息变成 500)。

    降级只影响**本进程本次**取到的载体;改对配置重启即可,不会永久停在降级态。
    """
    global _bus
    if _bus is None:
        from app.config.settings import settings

        backend = (getattr(settings, "collab_bus_backend", "sqlite") or "sqlite").lower()
        if backend == "pg":
            dsn = (getattr(settings, "collab_pg_dsn", "") or "").strip()
            if not dsn:
                logger.warning("collab_bus_backend=pg 但未配置 collab_pg_dsn,"
                               "回落 SQLite 载体")
            else:
                try:
                    from app.multi_agent.pg_event_bus import PostgresEventBus

                    bus = PostgresEventBus(dsn)
                    bus.init_schema()      # 建表失败要在这里暴露,而不是首次 publish 时
                    _bus = bus
                    logger.info("协作队列载体 = PostgreSQL(多实例安全:FOR UPDATE SKIP LOCKED)")
                    return _bus
                except Exception as exc:  # noqa: BLE001 见 docstring:不能打死买家链路
                    logger.warning(
                        "PG 队列载体初始化失败(%s: %s),回落 SQLite。"
                        "【运维须知】降级期间队列仍可用但**不具备多实例安全**:"
                        "多个 worker 会认领到同一批 id 再互相抢,表现为吞吐上不去"
                        "而非报错。请检查 collab_pg_dsn 与 PG 可达性。",
                        type(exc).__name__, exc)
        _bus = SqliteEventBus()
    return _bus


def set_event_bus(bus: Optional[EventBus]) -> None:
    """换载体。传 None 复位成默认(SQLite)——测试收尾用。"""
    global _bus
    _bus = bus


class InMemoryEventBus:
    """内存载体:**只用于测试**,不要在生产里注入。

    存在的理由有两个,都不是"跑得快"那么简单:

    1. **它证明 `EventBus` 接口真的能换实现。** 一个只有单一实现的接口是没被
       验证过的接口——你以为它抽干净了,直到真去写第二个实现才发现某个方法的
       语义只有 SQLite 那套讲得通。这个类是那次验证本身,而且它跑在 CI 里,
       接口哪天被改回"漏了 SQLite 细节"的样子,协作测试会当场红。
    2. 协作链的逻辑测试不必为了一张事件表去建库。

    **刻意保留的语义**(它们是接口契约的一部分,不是 SQLite 的实现细节):
    - `claim` 按 priority 降序、同优先级 FIFO,并原子地置 processing;
    - `finish` 只对仍是 processing 的记录生效(条件更新的等价物);
    - `reclaim_stale` 按 `consumed_at` 判超时。

    **刻意不做的**:持久化。所以它绝不能用于生产——这个类的存在不是"内存队列
    也可以",恰恰相反,总线的第一条设计取舍就是"持久化而非内存队列"。
    """

    def __init__(self):
        import threading

        self._rows: list[dict] = []
        self._next_id = 1
        self._lock = threading.Lock()

    def _now(self) -> str:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def publish(self, event_type: str, payload: dict, source: str, target: str,
                correlation_id: str, priority: int = 0,
                status: str = "pending") -> int:
        with self._lock:
            row = {"id": self._next_id, "event_type": event_type,
                   "payload": dict(payload or {}), "source_agent": source,
                   "target_agent": target, "correlation_id": correlation_id,
                   "status": str(status), "priority": int(priority),
                   "created_at": self._now(), "consumed_at": None}
            self._next_id += 1
            self._rows.append(row)
            return row["id"]

    def claim(self, target: str, limit: int = 20) -> list[dict]:
        with self._lock:
            pending = [r for r in self._rows
                       if r["target_agent"] == target and r["status"] == "pending"]
            pending.sort(key=lambda r: (-r["priority"], r["id"]))
            claimed = []
            for r in pending[:max(1, int(limit))]:
                r["status"] = "processing"
                r["consumed_at"] = self._now()
                claimed.append(dict(r))
            return claimed

    def finish(self, event_id: int, status: str) -> bool:
        with self._lock:
            for r in self._rows:
                if r["id"] == event_id and r["status"] == "processing":
                    r["status"] = status
                    return True
            return False

    def reclaim_stale(self, older_than_seconds: int = 300) -> int:
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(seconds=older_than_seconds)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            n = 0
            for r in self._rows:
                if r["status"] == "processing" and (r["consumed_at"] or "") <= cutoff:
                    r["status"] = "pending"
                    r["consumed_at"] = None
                    n += 1
            return n

    def timeline(self, correlation_id: Optional[str] = None,
                 limit: int = 100) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._rows
                    if correlation_id is None or r["correlation_id"] == correlation_id]
            return sorted(rows, key=lambda r: -r["id"])[:limit]

    def failed(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._rows if r["status"] == "failed"]
            return sorted(rows, key=lambda r: -r["id"])[:limit]

    def failed_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._rows if r["status"] == "failed")

    def retry(self, event_id: int) -> bool:
        with self._lock:
            for r in self._rows:
                if r["id"] == event_id and r["status"] == "failed":
                    r["status"] = "pending"
                    r["consumed_at"] = None
                    return True
            return False
