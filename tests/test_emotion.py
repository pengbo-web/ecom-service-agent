"""情绪:LLM 判定、非法值回落、按轮落库、进统计与告警。"""

from types import SimpleNamespace

import pytest

from app.agent import understanding as U
from app.db.database import Database


class FakeClient:
    def __init__(self, content):
        self._c = content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        msg = SimpleNamespace(content=self._c)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_parses_emotion():
    c = FakeClient('{"domain":"aftersale","intent":"投诉","need_kb":false,'
                   '"kb_query":null,"emotion":"angry","emotion_level":3}')
    qu = U.understand("你们太过分了", [], c, "m")
    assert qu.emotion == "angry" and qu.emotion_level == 3


def test_illegal_emotion_falls_back_to_neutral():
    """非法值不硬猜——与既有 domain 非法即 None 同口径。"""
    c = FakeClient('{"domain":"presale","intent":"商品咨询","need_kb":false,'
                   '"kb_query":null,"emotion":"狂怒","emotion_level":9}')
    qu = U.understand("这个多少钱", [], c, "m")
    assert qu.emotion == "neutral" and qu.emotion_level == 0


def test_missing_emotion_defaults_neutral():
    c = FakeClient('{"domain":"presale","intent":"商品咨询","need_kb":false,"kb_query":null}')
    qu = U.understand("这个多少钱", [], c, "m")
    assert qu.emotion == "neutral" and qu.emotion_level == 0


def test_rule_path_is_neutral():
    """规则快筛(零 LLM)的轮次不判情绪,保持 neutral 而不是漏字段。"""
    qu = U.understand("你好", [], FakeClient("{}"), "m")
    assert qu.source == "rule" and qu.emotion == "neutral"


def test_fallback_path_is_neutral():
    class Boom:
        chat = SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: (_ for _ in ()).throw(RuntimeError("llm down"))))
    qu = U.understand("一段比较长的正常问题,超过规则字数上限所以会走 LLM 分支", [], Boom(), "m")
    assert qu.source == "fallback" and qu.emotion == "neutral"


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    return d


def test_turn_signal_roundtrip(db):
    db.record_turn_signal("s1", "u1", "投诉", "angry", 3, True)
    rows = db.list_turn_signals(limit=5)
    assert rows[0]["emotion"] == "angry" and rows[0]["emotion_level"] == 3


def test_emotion_distribution(db):
    for e, lv in (("neutral", 0), ("neutral", 0), ("unhappy", 2), ("angry", 3)):
        db.record_turn_signal("s", "u", "其他", e, lv, False)
    d = db.emotion_distribution(window_days=7)
    assert d["total"] == 4
    assert d["counts"]["angry"] == 1
    assert d["angry_rate"] == pytest.approx(0.25)


def test_empty_distribution_is_zero_not_none(db):
    d = db.emotion_distribution(window_days=7)
    assert d["total"] == 0 and d["angry_rate"] == 0.0


def test_angry_rate_anomaly(db, monkeypatch):
    from app.agent.tools import anomaly, shop_analytics as sa
    monkeypatch.setattr(sa, "get_db", lambda: db)
    for _ in range(8):
        db.record_turn_signal("s", "u", "投诉", "angry", 3, False)
    for _ in range(2):
        db.record_turn_signal("s", "u", "其他", "neutral", 0, False)
    kinds = [a["kind"] for a in anomaly.anomaly_scan(window_days=7)["anomalies"]]
    assert "angry_rate_high" in kinds


def test_angry_below_min_samples_no_alarm(db, monkeypatch):
    from app.agent.tools import anomaly, shop_analytics as sa
    monkeypatch.setattr(sa, "get_db", lambda: db)
    db.record_turn_signal("s", "u", "投诉", "angry", 3, False)
    assert anomaly.anomaly_scan(window_days=7)["anomalies"] == []
