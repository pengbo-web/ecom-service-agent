"""确定性异常扫描:跨阈值才报、阈值可配、扫描不调 LLM、发布串同一条协作链。"""

import pytest

from app.agent.tools import anomaly
from app.agent.tools import shop_analytics as sa
from app.db.database import Database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    monkeypatch.setattr(sa, "get_db", lambda: d)
    from app.multi_agent import bus
    monkeypatch.setattr(bus, "get_db", lambda: d)
    return d


def _orders(d, sku, n, refunds, reason="尺码不准"):
    conn = d.connect()
    try:
        for i in range(n):
            oid = f"{sku}-{i}"
            conn.execute(
                "INSERT INTO orders (order_id,user,status,total,created_at,refund_status,"
                "refund_reason) VALUES (?,?,'pending',100,datetime('now'),?,?)",
                (oid, "u1", "requested" if i < refunds else None,
                 reason if i < refunds else None))
            conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                         "VALUES (?,?,?,1,100)", (oid, sku, sku))
        conn.commit()
    finally:
        conn.close()


def test_below_threshold_reports_nothing(db):
    _orders(db, "P001", 20, refunds=1)        # 5% < 默认 15%
    out = anomaly.anomaly_scan(window_days=7)
    assert out["success"] is True
    assert out["anomalies"] == []


def test_refund_rate_jump_is_reported(db):
    _orders(db, "P001", 20, refunds=6)        # 30% ≥ 15%
    out = anomaly.anomaly_scan(window_days=7)
    kinds = [a["kind"] for a in out["anomalies"]]
    assert "refund_rate_high" in kinds
    hit = [a for a in out["anomalies"] if a["kind"] == "refund_rate_high"][0]
    assert hit["subject"] == "P001"
    assert hit["value"] == pytest.approx(0.30)
    assert hit["threshold"] == pytest.approx(0.15)


def test_min_sample_guard_avoids_false_alarm(db):
    """样本太少不报:1 单退 1 单是 100%,但那不是"异常",是没数据。"""
    _orders(db, "P001", 1, refunds=1)
    assert anomaly.anomaly_scan(window_days=7)["anomalies"] == []


def test_tool_error_rate_reported(db):
    for _ in range(9):
        db.record_skill_trace("s", "u", "track-order", [], "tool_error")
    db.record_skill_trace("s", "u", "track-order", [], "success")
    kinds = [a["kind"] for a in anomaly.anomaly_scan(window_days=7)["anomalies"]]
    assert "tool_error_rate_high" in kinds


def test_threshold_is_configurable(db, monkeypatch):
    from app.config import settings as st
    _orders(db, "P001", 20, refunds=4)        # 20%
    monkeypatch.setattr(st.settings, "anomaly_refund_rate", 0.5)
    assert anomaly.anomaly_scan(window_days=7)["anomalies"] == []


def test_scan_does_not_call_llm(db, monkeypatch):
    """扫描必须是确定性的。任何 LLM 调用都让"是否异常"变得不可复现、要花钱。"""
    import openai

    def boom(*a, **k):
        raise AssertionError("anomaly_scan 不得调用 LLM")

    monkeypatch.setattr(openai.OpenAI, "__init__", boom)
    _orders(db, "P001", 20, refunds=6)
    assert anomaly.anomaly_scan(window_days=7)["anomalies"]


def test_scan_and_publish_shares_one_correlation_id(db):
    """同一次扫描出的多条异常属于同一条协作链,便于时间线聚合。"""
    _orders(db, "P001", 20, refunds=6)
    for _ in range(9):
        db.record_skill_trace("s", "u", "track-order", [], "tool_error")
    db.record_skill_trace("s", "u", "track-order", [], "success")
    out = anomaly.scan_and_publish(window_days=7)
    assert out["published"] >= 2
    events = db.list_events(correlation_id=out["correlation_id"])
    assert len(events) == out["published"]
    assert {e["target_agent"] for e in events} == {"analyst"}
