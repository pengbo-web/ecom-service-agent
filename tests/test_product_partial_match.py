"""回归:搜"红色 运动鞋"时库里只有黑色鞋,工具须诚实标注未命中的属性,
而不是把黑鞋当"红鞋"返回、诱导模型反复重搜(线上曾复现三次重搜后转人工)。

用隔离 DB(set_db),不依赖全局种子/测试顺序。
"""

import pytest

from app.db.database import Database
from app.db import set_db
from app.agent.tools.product import query_product, _match_score, _matched_keywords


@pytest.fixture
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    conn = d.connect()
    rows = [
        ("SHOE-BK", "Nike 运动鞋", "运动鞋", 899.0, 10, "透气缓震", '{"颜色": "黑色"}', 700.0),
        ("PHONE-BK", "某黑色手机", "手机", 3999.0, 10, "旗舰", '{"颜色": "黑色"}', 3500.0),
        ("BUD-WH", "白色耳机", "耳机", 199.0, 10, "降噪", '{"颜色": "白色"}', 150.0),
    ]
    for r in rows:
        conn.execute(
            "INSERT INTO products (product_id, name, category, price, stock, description, specs, floor_price) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", r,
        )
    conn.commit()
    conn.close()
    set_db(d)
    return d


def test_partial_match_flags_unmatched_color(db):
    r = query_product("红色 运动鞋")
    assert r["success"] is True
    assert r.get("partial_match") is True
    assert "红色" in r["unmatched_keywords"]          # 颜色没命中,被如实标出
    assert "运动鞋" not in r["unmatched_keywords"]    # 品类命中了
    assert "note" in r and "红色" in r["note"]
    assert any("运动鞋" in p["category"] for p in r["products"])  # 返回最接近的替代


def test_full_match_has_no_partial_flag(db):
    r = query_product("运动鞋")
    assert r["success"] is True
    assert "partial_match" not in r                   # 全部关键词命中 → 不标部分匹配
    assert r["products"][0]["product_id"] == "SHOE-BK"


def test_results_sorted_by_match_score(db):
    # "黑色 运动鞋":鞋命中2个,手机只命中1个 → 鞋排最前且独占最高档
    r = query_product("黑色 运动鞋")
    assert r["products"][0]["product_id"] == "SHOE-BK"
    assert all(p["product_id"] != "PHONE-BK" for p in r["products"])  # 弱匹配不掺入


def test_no_match_returns_empty_honestly(db):
    # 无匹配 → 空列表 + 诚实提示,绝不编造 mock 商品
    r = query_product("冰箱")
    assert r["success"] is True
    assert r["products"] == []
    assert "note" in r and "编造" in r["note"]


def test_matched_keywords_helper():
    prod = {"name": "Nike 运动鞋", "category": "运动鞋", "description": "", "specs": {"颜色": "黑色"}}
    assert set(_matched_keywords(prod, ["红色", "运动鞋"])) == {"运动鞋"}
    assert _match_score(prod, ["红色", "运动鞋"]) == 1
