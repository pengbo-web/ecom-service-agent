from app.db.database import Database


def _fresh_db(tmp_path):
    db = Database(db_path=str(tmp_path / "t.db"))
    db.init_schema()
    return db


def test_bargain_state_lifecycle(tmp_path):
    db = _fresh_db(tmp_path)
    assert db.get_bargain_state("s1", "P1") is None

    db.bump_bargain_state("s1", "P1", 900.0)
    st = db.get_bargain_state("s1", "P1")
    assert st["rounds"] == 1
    assert st["last_offer"] == 900.0

    db.bump_bargain_state("s1", "P1", 850.0)
    st = db.get_bargain_state("s1", "P1")
    assert st["rounds"] == 2
    assert st["last_offer"] == 850.0


def test_bargain_state_isolated_by_session_and_product(tmp_path):
    db = _fresh_db(tmp_path)
    db.bump_bargain_state("s1", "P1", 900.0)
    assert db.get_bargain_state("s2", "P1") is None
    assert db.get_bargain_state("s1", "P2") is None


def test_clear_bargain_state(tmp_path):
    db = _fresh_db(tmp_path)
    db.bump_bargain_state("s1", "P1", 900.0)
    db.bump_bargain_state("s1", "P2", 100.0)
    db.bump_bargain_state("s2", "P1", 500.0)  # 另一会话，clear s1 后应保留
    db.clear_bargain_state("s1")
    assert db.get_bargain_state("s1", "P1") is None
    assert db.get_bargain_state("s1", "P2") is None
    assert db.get_bargain_state("s2", "P1")["rounds"] == 1  # 未被误删


def test_product_floor_price_column(tmp_path):
    db = _fresh_db(tmp_path)
    conn = db.connect()
    conn.execute(
        "INSERT INTO products (product_id, name, category, price, stock, description, specs, floor_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("P1", "商品1", "cat", 1000.0, 5, "", "{}", 800.0),
    )
    conn.execute(
        "INSERT INTO products (product_id, name, category, price, stock, description, specs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("P2", "商品2", "cat", 1000.0, 5, "", "{}"),
    )
    conn.commit()
    conn.close()
    assert db.get_product("P1")["floor_price"] == 800.0
    assert db.get_product("P2")["floor_price"] is None
