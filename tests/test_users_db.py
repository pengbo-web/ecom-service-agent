from app.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    return db


def test_create_and_get_user(tmp_path):
    db = _db(tmp_path)
    assert db.create_user("小明", "小明") is True
    assert db.get_user("小明") == {"user_id": "小明", "name": "小明"}
    assert db.get_user("ghost") is None


def test_create_duplicate_returns_false_keeps_original(tmp_path):
    db = _db(tmp_path)
    db.create_user("u1", "原名")
    assert db.create_user("u1", "新名") is False
    assert db.get_user("u1")["name"] == "原名"
