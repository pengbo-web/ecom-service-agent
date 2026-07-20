from app.db import Database, get_db, set_db


def test_init_schema_creates_all_tables(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r["name"] for r in rows}
    finally:
        conn.close()
    assert {"products", "orders", "order_items",
            "shipments", "logistics_events", "users"} <= names


def test_connect_uses_row_factory(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    conn = db.connect()
    try:
        row = conn.execute("SELECT 1 AS x").fetchone()
        assert row["x"] == 1  # Row 支持按列名取值
    finally:
        conn.close()


def test_get_set_db_singleton(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    set_db(db)
    assert get_db() is db


def test_seed_from_mock_populates_tables(tmp_path):
    from app.db.seed import seed_from_mock
    from app.agent.tools.mock_data import ORDERS, PRODUCTS, LOGISTICS

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    seed_from_mock(db)

    conn = db.connect()
    try:
        n_orders = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        n_products = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
        n_events = conn.execute("SELECT COUNT(*) AS c FROM logistics_events").fetchone()["c"]
        n_items = conn.execute("SELECT COUNT(*) AS c FROM order_items").fetchone()["c"]
    finally:
        conn.close()

    assert n_orders == len(ORDERS)
    assert n_products == len(PRODUCTS)
    assert n_events == sum(len(v["events"]) for v in LOGISTICS.values())
    assert n_items == sum(len(o["items"]) for o in ORDERS.values())
