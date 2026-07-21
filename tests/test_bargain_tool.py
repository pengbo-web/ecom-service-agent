import pytest

from app.db.database import Database
from app.db import set_db
from app.agent.tools.bargain import negotiate_price, set_current_session


def _seed_products(db):
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


@pytest.fixture
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    _seed_products(d)
    set_db(d)
    set_current_session("t1")
    yield d
    set_current_session(None)


def test_accept_offer_and_bump_round(db):
    r = negotiate_price("P1", 950.0)
    assert r["success"] is True
    assert r["decision"] == "accept"
    assert r["suggested_price"] == 950.0
    assert r["round"] == 1
    assert db.get_bargain_state("t1", "P1")["rounds"] == 1


def test_reject_below_floor(db):
    r = negotiate_price("P1", 700.0)
    assert r["decision"] == "reject"
    assert r["suggested_price"] == 800.0
    assert r["floor_hit"] is True


def test_rounds_progress_across_calls(db):
    negotiate_price("P1", 810.0)   # round 1
    r = negotiate_price("P1", 810.0)  # round 2
    assert r["round"] == 2


def test_fallback_floor_when_no_explicit(db):
    # P2 无 floor_price → F=850, ladder0=925
    r = negotiate_price("P2", None)
    assert r["decision"] == "counter"
    assert r["suggested_price"] == 925.0


def test_unknown_product(db):
    r = negotiate_price("NOPE", 100.0)
    assert r["success"] is False


def test_disabled_returns_error(db, monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "bargain_enabled", False)
    r = negotiate_price("P1", 950.0)
    assert r["success"] is False
