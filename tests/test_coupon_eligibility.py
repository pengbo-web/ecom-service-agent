"""优惠券资格:数据源(member_level/订单数)+ 过滤。临时库,全离线。"""

from app.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    return db


def test_member_level_defaults_normal_and_updatable(tmp_path):
    db = _db(tmp_path)
    db.create_user("u1", "u1")
    assert db.get_user("u1")["member_level"] == "normal"      # 默认
    db.set_member_level("u1", "diamond")
    assert db.get_user("u1")["member_level"] == "diamond"


def test_get_user_missing_still_none(tmp_path):
    assert _db(tmp_path).get_user("ghost") is None


def test_count_user_orders(tmp_path):
    db = _db(tmp_path)
    conn = __import__("sqlite3").connect(db.db_path)
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O1','u1','pending',10,'t')")
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O2','u1','pending',20,'t')")
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O3','u2','pending',30,'t')")
    conn.commit(); conn.close()
    assert db.count_user_orders("u1") == 2
    assert db.count_user_orders("newbie") == 0           # 新客


def test_migration_idempotent_on_old_users_table(tmp_path):
    """旧库(users 无 member_level)init_schema 幂等加列,不炸、不丢数据。"""
    import sqlite3
    p = str(tmp_path / "old.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO users VALUES ('老用户','老用户')")
    conn.commit(); conn.close()
    db = Database(p); db.init_schema(); db.init_schema()   # 两次,验证幂等
    assert db.get_user("老用户")["member_level"] == "normal"   # 旧行补默认值
