"""参谋只读分析工具:口径正确、除零安全、空库不崩、绝不写库。"""

import pytest

from app.agent.tools import shop_analytics as sa
from app.db.database import Database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    monkeypatch.setattr(sa, "get_db", lambda: d)
    return d


def _seed(d):
    conn = d.connect()
    try:
        conn.execute("INSERT INTO products (product_id,name,category,price,stock) "
                     "VALUES ('P001','跑鞋','鞋类',399,10)")
        # 5 单,其中 2 单退款 → 退款率 40%
        for i, refund in enumerate(["", "", "", "requested", "requested"]):
            conn.execute(
                "INSERT INTO orders (order_id,user,status,total,created_at,refund_status,"
                "refund_reason) VALUES (?,?,?,?,datetime('now'),?,?)",
                (f"ORD-{i}", "u1", "pending", 399.0, refund,
                 "尺码不准" if refund else None))
            conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                         "VALUES (?, '跑鞋','P001',1,399)", (f"ORD-{i}",))
        conn.commit()
    finally:
        conn.close()


def test_overview_on_empty_db_does_not_crash(db):
    out = sa.shop_overview(window_days=7)
    assert out["success"] is True
    assert out["orders"] == 0
    assert out["refund_rate"] == 0.0          # 除零必须安全


def test_overview_counts_and_rates(db):
    _seed(db)
    out = sa.shop_overview(window_days=7)
    assert out["orders"] == 5
    assert out["gmv"] == pytest.approx(399.0 * 5)
    assert out["refund_rate"] == pytest.approx(0.4)
    assert out["avg_order_value"] == pytest.approx(399.0)


def test_overview_respects_window(db):
    _seed(db)
    conn = db.connect()
    try:
        conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                     "VALUES ('OLD','u1','pending',999,'2020-01-01 00:00:00')")
        conn.commit()
    finally:
        conn.close()
    assert sa.shop_overview(window_days=7)["orders"] == 5   # 窗外那单不计入


def test_product_diagnostics_surfaces_refund_reasons(db):
    _seed(db)
    out = sa.product_diagnostics(window_days=7, top_n=3)
    assert out["success"] is True
    top = out["products"][0]
    assert top["sku"] == "P001"
    assert top["refund_rate"] == pytest.approx(0.4)
    assert "尺码不准" in [r["reason"] for r in top["refund_reasons"]]


def test_service_quality_from_skill_traces(db):
    db.record_skill_trace("s1", "u1", "track-order", [], "success")
    db.record_skill_trace("s2", "u1", "track-order", [], "tool_error")
    db.record_skill_trace("s3", "u1", "track-order", [], "requires_human")
    out = sa.service_quality(window_days=7)
    assert out["success"] is True
    row = [r for r in out["skills"] if r["skill_name"] == "track-order"][0]
    assert row["total"] == 3
    assert row["success_rate"] == pytest.approx(1 / 3)
    assert row["human_rate"] == pytest.approx(1 / 3)


def test_tools_never_write(db):
    """参谋是只读 Agent:跑一遍全部工具后,库里的行数不能变。"""
    _seed(db)
    conn = db.connect()
    try:
        before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("orders", "order_items", "products", "skill_traces")}
    finally:
        conn.close()
    sa.shop_overview(); sa.product_diagnostics(); sa.service_quality()
    conn = db.connect()
    try:
        after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                 for t in ("orders", "order_items", "products", "skill_traces")}
    finally:
        conn.close()
    assert before == after
