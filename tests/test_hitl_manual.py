import itertools

from app.hitl.manual_mode import ManualMode
from app.hitl.manager import HitlManager
from app.hitl.queue import HandoffQueue


def test_toggle_and_is_manual():
    mm = ManualMode(timeout=3600, now=lambda: 100.0)
    assert mm.is_manual("s1") is False
    assert mm.toggle("s1") == "manual"
    assert mm.is_manual("s1") is True
    assert mm.toggle("s1") == "auto"
    assert mm.is_manual("s1") is False


def test_manual_mode_timeout():
    clock = {"t": 100.0}
    mm = ManualMode(timeout=60, now=lambda: clock["t"])
    mm.enter("s1")
    assert mm.is_manual("s1") is True
    clock["t"] = 100.0 + 61      # 超过 60s
    assert mm.is_manual("s1") is False   # 自动回落


def test_manager_evaluate_and_escalate(tmp_path):
    ids = itertools.count(1)
    q = HandoffQueue(str(tmp_path / "h.db"), id_factory=lambda: f"h{next(ids)}")
    q.init_schema()
    mgr = HitlManager(q, ManualMode(3600), confidence_threshold=0.6)
    reasons = mgr.evaluate("complaint", 0.9, False)
    assert reasons
    hid = mgr.escalate("s1", "投诉", "抱歉", "complaint", 0.9, reasons)
    assert q.get(hid)["session_id"] == "s1"
    assert q.count_pending() == 1
