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
