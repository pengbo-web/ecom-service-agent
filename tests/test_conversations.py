"""会话生命周期数据层:服务端签发 + open/closed 状态机。临时库,全离线。"""

from app.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    return db


def test_create_issues_server_id_and_open(tmp_path):
    db = _db(tmp_path)
    c = db.create_conversation("u1")
    assert c["conversation_id"].startswith("c-") and len(c["conversation_id"]) == 18
    assert c["status"] == "open" and c["user_id"] == "u1"
    got = db.get_conversation(c["conversation_id"])
    assert got["status"] == "open" and got["close_reason"] is None


def test_close_sets_reason_and_is_idempotent(tmp_path):
    db = _db(tmp_path)
    cid = db.create_conversation("u1")["conversation_id"]
    assert db.close_conversation(cid, "manual") is True
    got = db.get_conversation(cid)
    assert got["status"] == "closed" and got["close_reason"] == "manual"
    first_closed_at = got["closed_at"]
    assert db.close_conversation(cid, "idle") is False        # 已关:幂等,不覆盖
    got2 = db.get_conversation(cid)
    assert got2["close_reason"] == "manual" and got2["closed_at"] == first_closed_at
    assert db.close_conversation("c-notexist12345678", "idle") is False


def test_latest_open_per_user(tmp_path):
    db = _db(tmp_path)
    a = db.create_conversation("u1")["conversation_id"]
    db.close_conversation(a, "manual")
    b = db.create_conversation("u1")["conversation_id"]
    db.create_conversation("u2")
    assert db.latest_open_conversation("u1")["conversation_id"] == b
    db.close_conversation(b, "idle")
    assert db.latest_open_conversation("u1") is None


def test_list_conversations_desc_and_limit(tmp_path):
    db = _db(tmp_path)
    ids = [db.create_conversation("u1")["conversation_id"] for _ in range(3)]
    rows = db.list_conversations("u1", limit=2)
    assert len(rows) == 2
    assert rows[0]["conversation_id"] == ids[-1]      # 最新在前
    assert db.list_conversations("u_none") == []
