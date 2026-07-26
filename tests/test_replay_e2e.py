import sqlite3
import pytest
from app.api.streaming import run_agent_streaming
from app.agent.pending import PendingAction
from app.agent.runtime_context import set_current_user
from app.config.settings import settings
from app.db import Database, set_db

ORDER = "ORD-20240115-001"


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "e2e.db")); d.init_schema()
    conn = sqlite3.connect(d.db_path)
    conn.execute("INSERT INTO orders (order_id, user, status, total, created_at) "
                 "VALUES (?,?,?,?,?)", (ORDER, "xiaoming", "shipped", 899.0, "2024-01-15"))
    conn.commit(); conn.close()
    set_db(d)
    yield d
    set_db(None); set_current_user(None)


class _Agent:
    def __init__(self, user_id):
        self.raw_messages = []
        self.user_id = user_id
        self.client = None
        from app.agent.tools.manager import ToolManager
        self.tool_manager = ToolManager(use_mcp=False)
        self._pending = PendingAction(action="refund", tool_name="apply_refund",
                                      args={"order_id": ORDER, "reason": "no"},
                                      message="confirm")
    def save(self):
        pass


def _reply(events):
    r = [e for e in events if e["type"] == "reply"]
    return r[0]["content"] if r else ""


def test_owner(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_enabled", False)
    agent = _Agent("xiaoming")
    list(run_agent_streaming(agent, "queren dui " + ORDER + " tuikuan", session_id="s1"))
    assert db.get_order(ORDER)["status"] == "refund_processing"
    assert agent._pending is None


def test_non_owner(db, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "mcp_enabled", False)
    agent = _Agent("123")
    list(run_agent_streaming(agent, "queren dui " + ORDER + " tuikuan", session_id="s1"))
    assert db.get_order(ORDER)["status"] == "shipped"
