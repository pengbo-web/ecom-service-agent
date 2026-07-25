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


from app.agent.runtime_context import set_current_user, get_current_user
from app.agent.tools.order_ops import query_coupons, filter_coupons, _COUPONS


def test_filter_coupons_pure():
    # 新客+会员:新人券✓ 会员券✓ 全场券✓
    ok, no = filter_coupons(_COUPONS, is_new=True, is_member=True)
    codes = {c["code"] for c in ok}
    assert {"NEW20", "VIP90", "SHOE30"} <= codes and no == []
    # 老客+非会员:只剩全场券,另两张带原因
    ok, no = filter_coupons(_COUPONS, is_new=False, is_member=False)
    assert {c["code"] for c in ok} == {"SHOE30"}
    reasons = {c["code"]: c["reason"] for c in no}
    assert "NEW20" in reasons and "VIP90" in reasons


def test_query_coupons_filters_by_user(tmp_path, monkeypatch):
    from app.db import Database, set_db
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    db.create_user("newbie", "newbie")            # 新客,normal
    db.create_user("vip", "vip"); db.set_member_level("vip", "gold")
    import sqlite3
    conn = sqlite3.connect(db.db_path)            # vip 有 1 单 → 老客
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O1','vip','pending',10,'t')"); conn.commit(); conn.close()
    set_db(db)
    try:
        set_current_user("newbie")
        r = query_coupons()
        assert "NEW20" in {c["code"] for c in r["coupons"]}     # 新客有新人券
        assert "VIP90" not in {c["code"] for c in r["coupons"]} # 非会员无会员券
        set_current_user("vip")
        r = query_coupons()
        assert "VIP90" in {c["code"] for c in r["coupons"]}     # 会员有会员券
        assert "NEW20" not in {c["code"] for c in r["coupons"]} # 老客无新人券
        assert any(u["code"] == "NEW20" for u in r["unavailable"])
    finally:
        set_db(None); set_current_user(None)


def test_query_coupons_fail_open_without_user(tmp_path, monkeypatch):
    from app.db import Database, set_db
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    set_db(db)
    try:
        set_current_user(None)                    # 无当前用户
        r = query_coupons()
        assert len(r["coupons"]) == len(_COUPONS)  # fail-open:全部券
        assert r["unavailable"] == []
    finally:
        set_db(None)


def test_context_var_roundtrip():
    set_current_user("u9")
    assert get_current_user() == "u9"
    set_current_user(None)
    assert get_current_user() is None
