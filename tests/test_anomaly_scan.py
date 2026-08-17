"""确定性异常扫描:跨阈值才报、阈值可配、扫描不调 LLM、发布串同一条协作链。"""

import pytest

from app.db import set_db

from app.agent.tools import anomaly
from app.agent.tools import shop_analytics as sa
from app.db.database import Database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    monkeypatch.setattr(sa, "get_db", lambda: d)
    from app.multi_agent import bus
    set_db(d)
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
    """同一次扫描出的多条异常属于同一条协作链,便于时间线聚合。

    **`published` 数的是信号,`len(events)` 数的是投递记录,扇出之后两者不再相等。**
    `signal.anomaly` 现在有两个订阅者(见 `routing.SUBSCRIPTIONS`),而扇出发生在
    写入时——路由表算出 N 个订阅者就插 N 行。`publish` 一条信号仍只返回一个
    correlation_id,所以 `published` 仍然按信号计数。
    """
    _orders(db, "P001", 20, refunds=6)          # → refund_rate_high(subject 是 SKU)
    for _ in range(9):
        db.record_skill_trace("s", "u", "track-order", [], "tool_error")
    db.record_skill_trace("s", "u", "track-order", [], "success")
    out = anomaly.scan_and_publish(window_days=7)
    assert out["published"] >= 2
    events = db.list_events(correlation_id=out["correlation_id"])
    by_target: dict[str, list] = {}
    for e in events:
        by_target.setdefault(e["target_agent"], []).append(e)
    # 参谋订阅**所有**异常;风控只订阅 subject 是商品 SKU 的那些
    # (refund_rate_high / bad_review_rate_high),所以 tool_error_rate_high
    # 不该出现在 guard 那一侧。
    assert set(by_target) == {"analyst", "guard"}
    assert len(by_target["analyst"]) == out["published"]
    assert {e["payload"]["kind"] for e in by_target["guard"]} == {"refund_rate_high"}
    assert len(events) == out["published"] + len(by_target["guard"])


# ---- 边界值:>= 的两类比较都必须精确钉在边界上,不能悄悄退化成 > ----
# (退款率/两个 skill 比率的阈值边界,以及样本量 floor 边界)


def test_refund_rate_at_threshold_exactly_is_reported(db):
    """3/20 == 0.15,精确等于默认阈值——必须报,不能因为 >= 被改成 > 就漏判。"""
    _orders(db, "P001", 20, refunds=3)
    kinds = [a["kind"] for a in anomaly.anomaly_scan(window_days=7)["anomalies"]]
    assert "refund_rate_high" in kinds


def test_orders_at_min_samples_floor_exactly_is_eligible(db):
    """orders 恰好等于 min_samples(默认 5)时应当参与判断,不能因为
    `orders >= min_samples` 被改成 `>` 就把刚好卡在 floor 上的样本当成
    "样本不足"漏判。"""
    _orders(db, "P001", 5, refunds=4)         # orders == 5,80% 远超阈值,只考验 floor
    kinds = [a["kind"] for a in anomaly.anomaly_scan(window_days=7)["anomalies"]]
    assert "refund_rate_high" in kinds


def test_tool_error_rate_at_threshold_exactly_is_reported(db):
    """6/20 == 0.30,精确等于默认阈值。"""
    for _ in range(6):
        db.record_skill_trace("s", "u", "track-order", [], "tool_error")
    for _ in range(14):
        db.record_skill_trace("s", "u", "track-order", [], "success")
    kinds = [a["kind"] for a in anomaly.anomaly_scan(window_days=7)["anomalies"]]
    assert "tool_error_rate_high" in kinds


def test_human_rate_at_threshold_exactly_is_reported(db):
    """8/20 == 0.40,精确等于默认阈值。"""
    for _ in range(8):
        db.record_skill_trace("s", "u", "track-order", [], "handoff")
    for _ in range(12):
        db.record_skill_trace("s", "u", "track-order", [], "success")
    kinds = [a["kind"] for a in anomaly.anomaly_scan(window_days=7)["anomalies"]]
    assert "human_rate_high" in kinds


def test_total_at_min_samples_floor_exactly_is_eligible(db):
    """service_quality 侧 total 恰好等于 min_samples(默认 5)时应当参与判断,
    不能因为 `total < min_samples` 的判断被改动而把刚好卡在 floor 上的
    skill 漏判。"""
    for _ in range(4):
        db.record_skill_trace("s", "u", "track-order", [], "tool_error")
    db.record_skill_trace("s", "u", "track-order", [], "success")   # total == 5
    kinds = [a["kind"] for a in anomaly.anomaly_scan(window_days=7)["anomalies"]]
    assert "tool_error_rate_high" in kinds


# ---- product_diagnostics 只看前 PRODUCT_SCAN_LIMIT 名,超出部分必须显式亮出来 ----


def test_products_truncated_flag_when_shop_has_more_skus_than_slice(db, monkeypatch):
    """SKU 数超过切片大小时,products_truncated 必须置真:扫描只看了一部分,
    不能让返回值看起来像"全店都看过了"。"""
    monkeypatch.setattr(anomaly, "PRODUCT_SCAN_LIMIT", 3)
    for i in range(5):
        _orders(db, f"P{i:03d}", 2, refunds=0)
    out = anomaly.anomaly_scan(window_days=7)
    assert out["products_examined"] == 3
    assert out["products_truncated"] is True


def test_products_not_truncated_when_within_slice(db):
    """SKU 数没超过切片大小时,不应该被误报成"截断了"。"""
    _orders(db, "P001", 2, refunds=0)
    out = anomaly.anomaly_scan(window_days=7)
    assert out["products_examined"] == 1
    assert out["products_truncated"] is False
