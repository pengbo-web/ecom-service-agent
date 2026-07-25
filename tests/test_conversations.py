"""会话生命周期数据层:服务端签发 + open/closed 状态机。临时库,全离线。"""

from app.db import Database
from app.api.conversations import open_or_reuse, ensure_active


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


def test_open_or_reuse_returns_existing_open(tmp_path):
    db = _db(tmp_path)
    first = open_or_reuse(db, "u1")
    again = open_or_reuse(db, "u1")
    assert again["conversation_id"] == first["conversation_id"]   # 复用,不重复开
    db.close_conversation(first["conversation_id"], "manual")
    third = open_or_reuse(db, "u1")
    assert third["conversation_id"] != first["conversation_id"]   # 关了才翻篇


def test_ensure_active_open_passthrough(tmp_path):
    db = _db(tmp_path)
    cid = db.create_conversation("u1")["conversation_id"]
    assert ensure_active(db, cid, "u1") == (cid, False)


def test_ensure_active_closed_rotates(tmp_path):
    db = _db(tmp_path)
    cid = db.create_conversation("u1")["conversation_id"]
    db.close_conversation(cid, "idle")
    new_id, rotated = ensure_active(db, cid, "u1")
    assert rotated is True and new_id != cid and new_id.startswith("c-")


def test_ensure_active_never_adopts_client_id(tmp_path):
    """安全铁律:客户端自造/旧格式 ID 不被采纳,服务端换发。"""
    db = _db(tmp_path)
    new_id, rotated = ensure_active(db, "default--acbuu9p4", "u1")
    assert rotated is True and new_id.startswith("c-")
    assert db.get_conversation("default--acbuu9p4") is None      # 未被写库
