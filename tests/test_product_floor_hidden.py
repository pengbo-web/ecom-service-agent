"""回归测试：query_product 面向顾客，绝不能暴露 floor_price（议价底价）。"""

import pytest

from app.db.database import Database
from app.db import set_db
from app.agent.tools.product import query_product


@pytest.fixture
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    conn = d.connect()
    conn.execute(
        "INSERT INTO products (product_id, name, category, price, stock, description, specs, floor_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("SHOE-1", "测试运动鞋", "运动鞋", 899.0, 10, "透气", "{}", 750.0),
    )
    conn.commit()
    conn.close()
    set_db(d)
    return d


def test_exact_match_hides_floor_price(db):
    r = query_product("SHOE-1")
    assert r["success"] is True
    prod = r["products"][0]
    assert "floor_price" not in prod
    assert prod["price"] == 899.0  # 标价仍返回


def test_keyword_search_hides_floor_price(db):
    r = query_product("运动鞋")
    assert r["success"] is True
    assert all("floor_price" not in p for p in r["products"])
