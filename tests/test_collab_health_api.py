"""协作健康出口:失败事件可见 + 可重试,worker 心跳可判活。

补的是一个真实的可见性缺口:`bus.consume()` 刻意不自动重试 failed 事件,
注释说"留在表里供人工在时间线上看到并决定"——但时间线必须先知道
correlation_id 才查得到。在这个端点之前,一条失败的协作链没有任何人会发现。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

AUTH = {"X-Admin-Token": "T"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")

    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "health.db"))
    db.init_schema()
    set_db(db)

    from app.api.app import create_app
    c = TestClient(create_app())
    yield c
    set_db(None)


def _failed_event(db, corr="CX"):
    from app.multi_agent import bus
    eid = db.publish_event(bus.EV_SIGNAL_ANOMALY, {"kind": "refund_rate_high"},
                           bus.AGENT_SERVICE, bus.AGENT_ANALYST, corr)
    db.claim_events(bus.AGENT_ANALYST, limit=10)      # pending → processing
    db.finish_event(eid, "failed")
    return eid


def test_requires_auth(client):
    assert client.get("/api/admin/collab/health").status_code in (401, 403)


def test_failed_events_are_listed(client):
    from app.db import get_db
    db = get_db()
    assert client.get("/api/admin/collab/health", headers=AUTH).json()["failed_count"] == 0

    eid = _failed_event(db)
    body = client.get("/api/admin/collab/health", headers=AUTH).json()
    assert body["failed_count"] == 1
    assert [e["id"] for e in body["failed"]] == [eid]
    # 列出的信息要足够运营判断"这是哪条链上的什么事",而不只是一个 id
    row = body["failed"][0]
    assert row["correlation_id"] == "CX" and row["event_type"] and row["target_agent"]


def test_retry_puts_event_back_in_queue(client):
    """光看得见不够:运营看到之后必须能做点什么,否则"留给人工决定"里的
    "决定"是空的。"""
    from app.db import get_db
    from app.multi_agent import bus
    db = get_db()
    eid = _failed_event(db)

    r = client.post(f"/api/admin/collab/failed/{eid}/retry", headers=AUTH)
    assert r.json()["changed"] is True
    assert client.get("/api/admin/collab/health", headers=AUTH).json()["failed_count"] == 0
    # 真的回到了队列里:下一轮 worker 能重新认领
    assert [e["id"] for e in db.claim_events(bus.AGENT_ANALYST, limit=10)] == [eid]


def test_retry_is_idempotent(client):
    """连点两次 / 两个运营同时点:只有第一次真的改到状态。"""
    from app.db import get_db
    eid = _failed_event(get_db())
    assert client.post(f"/api/admin/collab/failed/{eid}/retry", headers=AUTH).json()["changed"]
    second = client.post(f"/api/admin/collab/failed/{eid}/retry", headers=AUTH).json()
    assert second["changed"] is False and "不在失败状态" in second["message"]


def test_worker_never_ran_is_not_healthy(client):
    """从未跑过 ≠ 跑过但停了:两者都判不健康,但要能分开提示
    (前者多半是没部署 worker,后者是部署了但挂了)。"""
    w = client.get("/api/admin/collab/health", headers=AUTH).json()["worker"]
    assert w["healthy"] is False and w["last_success_at"] is None


def test_heartbeat_marks_worker_healthy(client):
    from app.db import get_db
    from app.scripts.agent_collab import WORKER_NAME
    get_db().record_worker_heartbeat(WORKER_NAME, ok=True)
    w = client.get("/api/admin/collab/health", headers=AUTH).json()["worker"]
    assert w["healthy"] is True
    assert w["last_success_at"] and w["stale_seconds"] is not None
    assert w["stale_seconds"] <= w["threshold_seconds"]


def test_heartbeat_records_error_without_clearing_last_success(client):
    """成功与失败分两个字段记:只记成功的话,"每轮都抛异常的 worker"与
    "已经死掉的 worker"在看板上长得一模一样,而这两种故障处理方式不同。"""
    from app.db import get_db
    from app.scripts.agent_collab import WORKER_NAME
    db = get_db()
    db.record_worker_heartbeat(WORKER_NAME, ok=True)
    db.record_worker_heartbeat(WORKER_NAME, ok=False, error="扫描炸了")
    w = client.get("/api/admin/collab/health", headers=AUTH).json()["worker"]
    assert w["last_success_at"] is not None          # 上次成功的时间没被抹掉
    assert w["last_error"] == "扫描炸了" and w["last_error_at"]


def test_health_respects_seller_console_switch(client, monkeypatch):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "seller_console_enabled", False)
    assert client.get("/api/admin/collab/health", headers=AUTH).status_code == 404
