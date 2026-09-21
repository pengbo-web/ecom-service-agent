"""WS1 skill 记忆两层化:在线零决策标记(技术方案 §2)。

守三条:
1. 纠正模式表**宁漏勿错杀**——正常咨询/肯定语气/长句一个都不许命中;
2. kb_miss_policy 的每个分支(门控/意图/未走召回/有命中)判得对;
3. **hint 绝不改变归因判定**——判定权在 attribution 规则表,这是 41 条归属
   拦截实证换来的纪律,本文件用真实归因调用钉死它。
"""

from types import SimpleNamespace

import pytest

from app.agent.skills import memory_hints as mh
from app.agent.skills.attribution import CATEGORY_CAPABILITY_LIMIT, attribute
from app.config.settings import settings
from app.db.database import Database


# ---------- 纠正模式表 ----------

@pytest.mark.parametrize("text,expected", [
    ("不对", "denial"),
    ("不对。", "denial"),
    ("不对,我问的是运费", "denial_then_fix"),
    ("你说错了", "you_said_wrong"),
    ("不是这个意思", "not_this_meaning"),
    ("我问的是退款进度", "i_asked"),
    ("你搞错了", "you_mixed_up"),
    ("答非所问", "irrelevant_answer"),
])
def test_correction_positives(text, expected):
    assert mh.detect_user_correction(text) == expected


@pytest.mark.parametrize("text", [
    "这个尺寸不对吗",          # 正常咨询,子串含"不对"但不该命中
    "不对吧,我觉得挺好的",     # 肯定语气
    "人工审核要多久",
    "好的",
    "",
    "不对" * 12,               # 超 20 字门限:长句不收,宁漏勿错杀
    "我问的是退款进度吗你们到底有没有人在看这个问题啊",  # 24 字,超门限
])
def test_correction_negatives(text):
    assert mh.detect_user_correction(text) is None


# ---------- kb_miss_policy ----------

def _qu(need_kb=True, intent="政策咨询"):
    return SimpleNamespace(need_kb=need_kb, intent=intent)


def _recall(hits=None, backend="local"):
    return SimpleNamespace(kb_hits=hits or [], kb_backend=backend)


def test_kb_miss_policy_hit():
    assert mh.detect_kb_miss_policy(_qu(), _recall()) is True


def test_kb_miss_policy_negatives():
    assert mh.detect_kb_miss_policy(_qu(need_kb=False), _recall()) is False
    assert mh.detect_kb_miss_policy(_qu(intent="订单事务"), _recall()) is False
    assert mh.detect_kb_miss_policy(_qu(), None) is False          # 本轮没走统一召回(FAQ 秒答等)
    assert mh.detect_kb_miss_policy(_qu(), _recall(hits=[{"t": "x"}])) is False  # 有依据=正确行为
    assert mh.detect_kb_miss_policy(None, _recall()) is False


# ---------- 落库 / 报数 ----------

def test_record_and_stats_idempotent(tmp_path):
    d = Database(db_path=str(tmp_path / "h.db"))
    d.init_schema()
    mh.record_hint("s1", "track-order", mh.KIND_USER_CORRECTION, detail="denial", db=d)
    mh.record_hint("s1", "track-order", mh.KIND_USER_CORRECTION, detail="denial", db=d)  # 同轮重跑幂等
    mh.record_hint("s2", "track-order", mh.KIND_KB_MISS_POLICY, db=d)
    mh.record_hint("s3", "query-coupons", mh.KIND_USER_CORRECTION, db=d)
    stats = mh.hint_stats(db=d)
    assert stats["track-order"][mh.KIND_USER_CORRECTION] == 1
    assert stats["track-order"][mh.KIND_KB_MISS_POLICY] == 1
    assert stats["query-coupons"][mh.KIND_USER_CORRECTION] == 1
    assert mh.hint_stats("track-order", db=d) == stats["track-order"]


# ---------- 不变量:hint 不改变归因 ----------

def _ownership_trace():
    return {
        "session_id": "s1", "user_id": "ab0", "skill_name": "track-order",
        "outcome": "tool_error",
        "tool_calls": [{"name": "query_order", "ok": False,
                        "error": "未找到订单 ORD-20240115-001",
                        "args": {"order_id": "ORD-20240115-001"}}],
    }


def test_hint_does_not_change_attribution(tmp_path, monkeypatch):
    """归属拒绝的轨迹,无论该会话带不带 hint,归因都必须是 capability_limit。

    hint 只是标记:采样排序与报数用它,判定不用它。若未来有人把 hint 提进
    归因判据,这条测试会红。
    """
    monkeypatch.setattr(settings, "auth_enabled", True)
    d = Database(db_path=str(tmp_path / "inv.db"))
    d.init_schema()
    conn = d.connect()
    try:
        conn.execute(
            "INSERT INTO orders (order_id,user,status,total,created_at) "
            "VALUES ('ORD-20240115-001','小明','shipped',899,datetime('now'))")
        conn.commit()
    finally:
        conn.close()

    trace = _ownership_trace()
    before = attribute(trace, db=d)
    mh.record_hint("s1", "track-order", mh.KIND_USER_CORRECTION, db=d)
    mh.record_hint("s1", "track-order", mh.KIND_KB_MISS_POLICY, db=d)
    after = attribute(trace, db=d)

    assert before["rule"] == after["rule"] == "ownership_denied"
    assert before["category"] == after["category"] == CATEGORY_CAPABILITY_LIMIT


# ---------- chat 接线:fail-soft 与归因对象 ----------

def _bare_agent():
    from app.agent.chat import EcomAgent
    agent = EcomAgent.__new__(EcomAgent)   # 跳过 __init__:接线方法只依赖下列属性
    agent.session_id = "s1"
    agent._last_skill_name = "track-order"
    agent._turn_qu = None
    agent._turn_recall = None
    agent._skill_turn = None
    return agent


def test_record_memory_hints_swallows_exceptions(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("埋点炸了")
    monkeypatch.setattr(mh, "detect_user_correction", lambda t: "denial")
    monkeypatch.setattr(mh, "record_hint", boom)
    _bare_agent()._record_memory_hints("不对")   # 不许冒泡


def test_record_memory_hints_targets(monkeypatch):
    calls = []
    monkeypatch.setattr(mh, "record_hint",
                        lambda sid, skill, kind, **kw: calls.append((sid, skill, kind)))
    agent = _bare_agent()
    agent._turn_qu = _qu()
    agent._turn_recall = ("退货运费谁出", _recall())
    agent._skill_turn = SimpleNamespace(has_skill=True, skill_name="process-return")

    agent._record_memory_hints("退货运费谁出")   # 非纠正句:只应产 kb_miss_policy
    assert calls == [("s1", "process-return", mh.KIND_KB_MISS_POLICY)]

    calls.clear()
    agent._record_memory_hints("不对")           # 纠正句:归**上一轮**的 skill
    kinds = {c[2] for c in calls}
    assert ("s1", "track-order", mh.KIND_USER_CORRECTION) in calls
    assert mh.KIND_KB_MISS_POLICY in kinds
