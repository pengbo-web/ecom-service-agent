"""订单归属校验:auth 门控 + 隐私 fail-closed。临时库,全离线。"""

import sqlite3
import pytest

from app.agent.runtime_context import set_current_user
from app.agent.tools.ownership import owned_order
from app.config.settings import settings
from app.db import Database, set_db


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "t.db")); d.init_schema()
    conn = sqlite3.connect(d.db_path)
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O-A','alice','pending',10,'t')")
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES ('O-B','bob','shipped',20,'t')")
    conn.commit(); conn.close()
    set_db(d)
    yield d
    set_db(None); set_current_user(None)


def test_missing_order_returns_none(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert owned_order("O-NONE") is None


def test_auth_off_passes_through(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    set_current_user(None)
    assert owned_order("O-B")["order_id"] == "O-B"   # 教学放行,不校验


def test_owner_gets_order(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert owned_order("O-A")["order_id"] == "O-A"


def test_foreign_order_denied_as_none(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert owned_order("O-B") is None                # bob 的单,alice 越权→None


def test_auth_on_no_user_denied(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user(None)
    assert owned_order("O-A") is None                # 隐私 fail-closed


def test_query_order_blocks_foreign(db, monkeypatch):
    from app.agent.tools.order import query_order
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    r = query_order("O-B")                            # bob 的单
    assert r["success"] is False                      # 视同未找到
    r2 = query_order("O-A")                            # 自己的
    assert r2["success"] is True


def test_apply_refund_blocks_foreign(db, monkeypatch):
    from app.agent.tools.refund import apply_refund
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert apply_refund("O-B", "不想要了")["success"] is False   # 越权退款被拒


def test_change_address_blocks_foreign(db, monkeypatch):
    from app.agent.tools.order_ops import change_address
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert change_address("O-B", "新地址")["success"] is False


def test_query_logistics_blocks_foreign(db, monkeypatch):
    from app.agent.tools.logistics import query_logistics
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    assert query_logistics("O-B")["success"] is False


def test_list_user_orders_filters_by_current_user(db, monkeypatch):
    from app.agent.tools.user_orders import list_user_orders
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user("alice")
    r = list_user_orders()
    ids = {o["order_id"] for o in r["orders"]}
    assert ids == {"O-A"} and r["count"] == 1          # 只见自己的


def test_list_user_orders_auth_off_returns_all(db, monkeypatch):
    from app.agent.tools.user_orders import list_user_orders
    monkeypatch.setattr(settings, "auth_enabled", False)
    set_current_user(None)
    r = list_user_orders()
    assert r["count"] == 2                              # 教学放行,全量


def test_auth_on_no_user_list_empty(db, monkeypatch):
    from app.agent.tools.user_orders import list_user_orders
    monkeypatch.setattr(settings, "auth_enabled", True)
    set_current_user(None)
    assert list_user_orders()["count"] == 0            # 隐私 fail-closed
