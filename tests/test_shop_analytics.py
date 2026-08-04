"""参谋只读分析工具:口径正确、除零安全、空库不崩、绝不写库。"""

import pytest

from app.agent.skills.execution_trace import (
    OUTCOME_HANDOFF,
    OUTCOME_SUCCESS,
    OUTCOME_TOOL_ERROR,
)
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
    # _seed 已经写入 2 单 "尺码不准" 退款;这里额外加 1 单不同原因的退款,
    # 让两个原因的计数不相等——否则"按 n DESC 排序"和"随便什么顺序"在
    # 单一原因的样本下没有区别,测不出 ORDER BY 是不是真的生效了。
    _seed(db)
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO orders (order_id,user,status,total,created_at,refund_status,"
            "refund_reason) VALUES ('ORD-5','u1','pending',399.0,datetime('now'),"
            "'requested','发错货')")
        conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                     "VALUES ('ORD-5','跑鞋','P001',1,399)")
        conn.commit()
    finally:
        conn.close()

    out = sa.product_diagnostics(window_days=7, top_n=3)
    assert out["success"] is True
    top = out["products"][0]
    assert top["sku"] == "P001"
    assert top["refund_rate"] == pytest.approx(3 / 6)
    reasons = [r["reason"] for r in top["refund_reasons"]]
    counts = [r["count"] for r in top["refund_reasons"]]
    # "尺码不准" 计数 2 > "发错货" 计数 1,ORDER BY n DESC 必须把它排在前面
    assert reasons == ["尺码不准", "发错货"]
    assert counts == [2, 1]


def test_service_quality_from_skill_traces(db):
    # 必须用 execution_trace 里的 OUTCOME_* 常量落种子数据,而不是自己写字面量
    # ("requires_human" 之类)——否则这个测试测的是"种子数据能不能匹配 SQL
    # 里的字面量",而不是"SQL 能不能匹配生产真实写入的值",两者一旦分道
    # 扬镳,测试还会绿。
    db.record_skill_trace("s1", "u1", "track-order", [], OUTCOME_SUCCESS)
    db.record_skill_trace("s2", "u1", "track-order", [], OUTCOME_TOOL_ERROR)
    db.record_skill_trace("s3", "u1", "track-order", [], OUTCOME_HANDOFF)
    out = sa.service_quality(window_days=7)
    assert out["success"] is True
    row = [r for r in out["skills"] if r["skill_name"] == "track-order"][0]
    assert row["total"] == 3
    assert row["success_rate"] == pytest.approx(1 / 3)
    assert row["tool_error_rate"] == pytest.approx(1 / 3)
    assert row["human_rate"] == pytest.approx(1 / 3)
    assert row["other"] == 0


def test_service_quality_unknown_outcome_counted_as_other(db):
    # outcome 既不是 success/tool_error/handoff 三者之一(脏数据、未来新增取值)
    # 时:仍计入 total,但不计入任何一个 rate 的分子——三个 rate 因此不保证
    # 求和为 1,这里用 "other" 把这部分显式带出来,而不是让它悄悄消失。
    db.record_skill_trace("s1", "u1", "track-order", [], OUTCOME_SUCCESS)
    db.record_skill_trace("s2", "u1", "track-order", [], "some_future_outcome")
    out = sa.service_quality(window_days=7)
    row = [r for r in out["skills"] if r["skill_name"] == "track-order"][0]
    assert row["total"] == 2
    assert row["success_rate"] == pytest.approx(0.5)
    assert row["tool_error_rate"] == 0.0
    assert row["human_rate"] == 0.0
    assert row["other"] == 1


def _snapshot(conn, tables):
    """每张表的完整行内容(而非只有行数)的快照,按主键排序保证可比较。

    只比行数会漏掉 UPDATE:改了某一行某一列,行数前后不变,COUNT(*) 测不出来。
    这里连列值一起拍下来,UPDATE/INSERT/DELETE 任何一种写操作都会让快照不等。
    """
    snap = {}
    for t in tables:
        rows = conn.execute(f"SELECT * FROM {t}").fetchall()
        snap[t] = sorted(tuple(row) for row in rows)
    return snap


def test_tools_never_write(db):
    """参谋是只读 Agent:跑一遍全部工具后,库里每张表的行内容(不只是行数)不能变。"""
    _seed(db)
    tables = ("orders", "order_items", "products", "skill_traces")
    conn = db.connect()
    try:
        before = _snapshot(conn, tables)
    finally:
        conn.close()
    sa.shop_overview(); sa.product_diagnostics(); sa.service_quality()
    conn = db.connect()
    try:
        after = _snapshot(conn, tables)
    finally:
        conn.close()
    assert before == after
