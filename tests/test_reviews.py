"""评价:只有已签收可评、一单一 sku 一次、差评分析只读、跨线告警。"""

import pytest

from app.db.database import Database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    from app.agent.tools import reviews as rv
    monkeypatch.setattr(rv, "get_db", lambda: d)
    return d


def _order(d, oid, user, status, sku="P001", name="跑鞋"):
    conn = d.connect()
    try:
        conn.execute("INSERT OR REPLACE INTO products (product_id,name,category,price,stock) "
                     "VALUES (?,?,'鞋类',899,10)", (sku, name))
        conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                     "VALUES (?,?,?,899,datetime('now'))", (oid, user, status))
        conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                     "VALUES (?,?,?,1,899)", (oid, name, sku))
        conn.commit()
    finally:
        conn.close()


def test_create_and_list(db):
    _order(db, "O1", "u1", "delivered")
    rid = db.create_review("O1", "u1", "P001", 5, "很合脚")
    assert rid and db.list_reviews()[0]["rating"] == 5


def test_duplicate_review_returns_none_not_raise(db):
    """一单一 sku 只能评一次;重复要给明确结果而不是抛异常。"""
    _order(db, "O1", "u1", "delivered")
    assert db.create_review("O1", "u1", "P001", 5, "好") is not None
    assert db.create_review("O1", "u1", "P001", 1, "改成差评") is None


def test_reviewable_only_delivered(db):
    """没收到货就能评分是假数据。"""
    _order(db, "O1", "u1", "delivered")
    _order(db, "O2", "u1", "pending")
    items = db.reviewable_items("u1")
    assert [i["order_id"] for i in items] == ["O1"]


def test_reviewed_item_drops_out_of_reviewable(db):
    _order(db, "O1", "u1", "delivered")
    db.create_review("O1", "u1", "P001", 4, "还行")
    assert db.reviewable_items("u1") == []


def test_stats_empty_is_zero_not_none(db):
    s = db.review_stats(window_days=7)
    assert s["total"] == 0 and s["avg_rating"] == 0.0 and s["bad_rate"] == 0.0


def test_bad_rate(db):
    for i, r in enumerate([5, 4, 2, 1, 1]):
        _order(db, f"O{i}", "u1", "delivered")
        db.create_review(f"O{i}", "u1", "P001", r, "内容")
    s = db.review_stats(window_days=7)
    assert s["total"] == 5
    assert s["bad_rate"] == pytest.approx(0.6)      # rating<=2 的 3 条


def test_insights_surfaces_bad_terms_from_vocabulary(db):
    """差评关键词用词表匹配,不引分词依赖(见方案约束6)。"""
    from app.agent.tools.reviews import review_insights
    for i in range(5):
        _order(db, f"O{i}", "u1", "delivered")
        db.create_review(f"O{i}", "u1", "P001", 1, "跑鞋尺码偏大,想退款,还要我承担运费")
    out = review_insights(window_days=7)
    assert out["success"] is True
    p = out["products"][0]
    assert p["sku"] == "P001"
    assert p["bad_count"] == 5
    assert p["bad_terms"]                       # 至少撞上"跑鞋"/"退款"/"运费"之一
    assert all(not any(ch.isdigit() for ch in t) for t in p["bad_terms"])


def test_insights_excludes_products_without_bad_reviews(db):
    """零差评的商品不进「差评 top 商品」。

    实跑走查里,全店没有差评时经营控制台显示的是:
    「差评 top 商品:验收跑鞋 · 均分 5.0 · 差评 0 条」——标题和内容自相矛盾。
    更糟的是参谋读到这份数据会把一件毫无问题的商品当成"差评最多的商品"去归因,
    再据此起草触达。宁可返回空列表(前端有空态)。
    """
    from app.agent.tools.reviews import review_insights

    for i in range(3):                       # 全是好评
        _order(db, f"G{i}", "u1", "delivered")
        db.create_review(f"G{i}", "u1", "P001", 5, "很好")
    out = review_insights(window_days=7)
    assert out["total"] == 3                 # 评价统计照常
    assert out["products"] == [], "没有差评就不该有『差评 top 商品』"


def test_insights_keeps_only_products_with_bad_reviews(db):
    """混合情况:只保留真有差评的那些,不因为要凑满 top_n 就把好评商品填进来。"""
    from app.agent.tools.reviews import review_insights

    _order(db, "B1", "u1", "delivered")
    db.create_review("B1", "u1", "P001", 1, "很差")
    _order(db, "G1", "u1", "delivered")
    db.create_review("G1", "u1", "P002", 5, "很好")

    out = review_insights(window_days=7, top_n=5)
    assert [p["sku"] for p in out["products"]] == ["P001"]


def test_insights_never_writes(db):
    from app.agent.tools.reviews import review_insights
    _order(db, "O1", "u1", "delivered")
    db.create_review("O1", "u1", "P001", 1, "差")
    conn = db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"]
    finally:
        conn.close()
    review_insights(window_days=7)
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == before
    finally:
        conn.close()


def test_review_tool_is_seller_only(db):
    """全店差评数据不能进买家会话。"""
    from app.agent.tools.registry import SELLER_ONLY_TOOLS
    from app.multi_agent.agents import AGENT_CONFIGS
    assert "review_insights" in SELLER_ONLY_TOOLS
    for cfg in AGENT_CONFIGS.values():
        assert "review_insights" not in cfg["tools"]


def test_refunded_order_review_still_counts_toward_bad_rate(db):
    """决策(review finding 2):退款不会让已写的评价从差评率里消失。

    评价记的是买家收货那一刻的真实体验,后续退款是另一件事,不能让"退款"
    变成把差评从统计里洗掉的手段——那正是差评率与它驱动的告警要抓的东西。
    见 Database.review_stats 的 docstring。"""
    _order(db, "O1", "u1", "delivered")
    rid = db.create_review("O1", "u1", "P001", 1, "质量太差,已申请退款")
    assert rid is not None
    assert db.set_refund("O1", "质量问题申请退款") is True

    s = db.review_stats(window_days=7)
    assert s["total"] == 1 and s["bad_rate"] == pytest.approx(1.0)

    from app.agent.tools.reviews import review_insights
    out = review_insights(window_days=7)
    assert out["bad_rate"] == pytest.approx(1.0)
    assert out["products"][0]["bad_count"] == 1


def test_bad_review_anomaly(db, monkeypatch):
    from app.agent.tools import anomaly, shop_analytics as sa
    monkeypatch.setattr(sa, "get_db", lambda: db)
    from app.agent.tools import reviews as rv
    monkeypatch.setattr(rv, "get_db", lambda: db)
    for i in range(6):
        _order(db, f"O{i}", "u1", "delivered")
        db.create_review(f"O{i}", "u1", "P001", 1 if i < 4 else 5, "尺码偏大")
    kinds = [a["kind"] for a in anomaly.anomaly_scan(window_days=7)["anomalies"]]
    assert "bad_review_rate_high" in kinds
