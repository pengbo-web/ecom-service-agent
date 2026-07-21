from app.db.database import Database
from app.db.seed import seed_from_mock


def test_seed_carries_floor_price(tmp_path):
    db = Database(db_path=str(tmp_path / "t.db"))
    db.init_schema()
    seed_from_mock(db)
    # mock_data 中 SHOE 设了 floor_price=750，AirPods 未设
    assert db.get_product("SHOE-270-BK-42")["floor_price"] == 750.0
    assert db.get_product("ELEC-APP-002")["floor_price"] is None
