import itertools
from app.hitl.queue import HandoffQueue


def _q(tmp_path):
    ids = itertools.count(1)
    q = HandoffQueue(str(tmp_path / "h.db"), id_factory=lambda: f"h{next(ids)}")
    q.init_schema()
    return q


def test_add_and_list_pending(tmp_path):
    q = _q(tmp_path)
    hid = q.add({"session_id": "s1", "intent": "complaint", "reasons": ["x"]})
    assert hid == "h1"
    pending = q.list_pending()
    assert len(pending) == 1
    assert pending[0]["session_id"] == "s1"
    assert pending[0]["status"] == "pending"
    assert pending[0]["intent"] == "complaint"


def test_resolve(tmp_path):
    q = _q(tmp_path)
    hid = q.add({"session_id": "s1", "reasons": []})
    assert q.resolve(hid) is True
    assert q.count_pending() == 0
    assert q.list_pending() == []


def test_resolve_missing(tmp_path):
    q = _q(tmp_path)
    assert q.resolve("nope") is False


def test_get_returns_bundle(tmp_path):
    q = _q(tmp_path)
    hid = q.add({"session_id": "s2", "user_input": "退款", "reasons": ["敏感意图"]})
    got = q.get(hid)
    assert got["session_id"] == "s2"
    assert got["payload"]["user_input"] == "退款"
