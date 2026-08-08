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


def test_inconsistent_neutral_with_high_level_falls_back():
    """Finding-2:emotion="neutral" 但 emotion_level=3——标签与强度自相矛盾。
    两个字段各自看都合法(neutral 在白名单/3 在 0..3 区间内),但组合不自洽:
    若放过,前端 MetadataChips 直接消费 emotion 标签就会把"中性"渲染成
    没有情绪标(因为标签是 neutral),但落库的 emotion_level 却是 3,统计侧会
    出现"neutral 却计入高强度"的错乱。按既有"非法即整体回落"同口径处理。"""
    c = FakeClient('{"domain":"presale","intent":"商品咨询","need_kb":false,'
                   '"kb_query":null,"emotion":"neutral","emotion_level":3}')
    qu = U.understand("这个多少钱", [], c, "m")
    assert qu.emotion == "neutral" and qu.emotion_level == 0


def test_inconsistent_angry_label_with_zero_level_falls_back():
    """Finding-2:反方向——emotion="angry" 但 emotion_level=0。同样自相矛盾
    (angry 语义上要求 level 达到激烈阈值),整体回落 neutral/0,不保留
    "angry" 标签(否则前端会渲染出"情绪激烈"标,但强度其实是 0)。"""
    c = FakeClient('{"domain":"aftersale","intent":"投诉","need_kb":false,'
                   '"kb_query":null,"emotion":"angry","emotion_level":0}')
    qu = U.understand("随便问问", [], c, "m")
    assert qu.emotion == "neutral" and qu.emotion_level == 0


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


def test_faq_cache_hit_still_records_turn_signal(db, tmp_path, monkeypatch):
    """Finding-1(CRITICAL):FAQ 秒答分支是 chat() 里唯一的提前 return——
    已核实全函数再没有其它早退路径(唯二的 return 就是这里和末尾正常路径,
    互斥,不会重复落库)。FAQ 缓存命中的前提是 need_kb=True,即本轮已经过
    LLM 查询理解、情绪已经判定,过去直接在这里 return 掉,跳过了末尾的
    _record_turn_signal,判定结果被白白丢弃——这正是 brief 里点出的"分母
    失真"那类轮次。这里驱动一轮真实走 FAQ 缓存命中路径,断言 turn_signals
    真的落了一行,且情绪就是本轮 QU 判定的那个值。"""
    from app.config.settings import settings
    from app.agent.chat import EcomAgent

    monkeypatch.setattr(settings, "faq_cache_enabled", True)
    monkeypatch.setattr(settings, "emotion_trace_enabled", True)
    monkeypatch.setattr("app.db.get_db", lambda: db)
    from app.agent.faq_cache import FaqLookupOutcome
    monkeypatch.setattr(
        "app.agent.faq_cache.get_faq_cache",
        lambda: SimpleNamespace(lookup_with_state=lambda q: FaqLookupOutcome(
            state="hit", hit={"question": "下单后多久发货", "answer": "48小时内出库", "score": 0.95})),
    )

    agent = EcomAgent(session_path=str(tmp_path / "s.json"), session_id="s-faq-1",
                      user_id="u-faq-1")
    monkeypatch.setattr(agent, "_write_snapshot", lambda: None)   # 隔离:不落真实快照库
    # FAQ 命中理应零 LLM 短路;若走到这里说明短路失效,测试本身也该报错暴露出来
    monkeypatch.setattr(agent, "_react_loop",
                        lambda: (_ for _ in ()).throw(AssertionError("FAQ 命中不该走到 LLM")))
    agent.set_turn_understanding(U.QueryUnderstanding(
        intent="投诉", need_kb=True, kb_query="下单多久发货", source="llm",
        emotion="angry", emotion_level=3))

    result = agent.chat("下单多久能发货?")
    assert "48小时内出库" in result.reply   # FAQ 秒答本身没坏(前置断言)

    rows = db.list_turn_signals(limit=5)
    assert rows and rows[0]["emotion"] == "angry" and rows[0]["emotion_level"] == 3
