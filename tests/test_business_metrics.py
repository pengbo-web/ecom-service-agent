"""业务效果报表:真实流量上的自助解决率 / 转人工率 / 轮次 / 差评率。

补的是评估体系的一个结构性缺口:`evaluator.py` 产出的是模型与流程的质量
(`pass_rate` / `avg_process_score`),不是客服业务的效果。两者刻意分成两份报表——
沙箱里没有真实用户,算不出转人工率。

本文件钉三件事:
1. **只认 `DECISION_SOURCES`**,与看门狗判定同一处常量;
2. **轮级与会话级是两个口径**,不能互相换算;
3. **样本不足给 None 并登记原因**,不凑一个无意义的比率。
"""

from datetime import datetime, timedelta

import pytest

from app.db.database import Database
from app.evaluation import business_metrics as bm


def _ts(**kw):
    return (datetime.now() + timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    return d


def _trace(d: Database, session: str, outcome: str, source: str = "live",
           created_at: str = None, skill: str = "track-order"):
    conn = d.connect()
    try:
        conn.execute(
            "INSERT INTO skill_traces (session_id, user_id, skill_name, tool_calls, "
            "outcome, created_at, source) VALUES (?,?,?,?,?,?,?)",
            (session, "u1", skill, "[]", outcome, created_at or _ts(), source))
        conn.commit()
    finally:
        conn.close()


_REVIEW_SEQ = [0]


def _review(d: Database, rating: int, created_at: str = None):
    """插一条评价。**order_id 必须每次不同**——`reviews` 上有
    `(order_id, sku)` 唯一约束(一单一商品只能评一次),写死同一个 id 会让第二条
    插入直接 IntegrityError。"""
    _REVIEW_SEQ[0] += 1
    conn = d.connect()
    try:
        conn.execute(
            "INSERT INTO reviews (order_id, user_id, sku, rating, content, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (f"O-{_REVIEW_SEQ[0]}", "u1", "HMDP-1", rating, "还行",
             created_at or _ts()))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------- 只认真实流量

def test_only_live_traffic_counts_towards_decisions(db):
    """与看门狗判定同一口径。压测/评测/走查流量算进业务指标的代价比算进看门狗更
    隐蔽:它不触发任何自动动作,只让人对系统形成一个错的判断,然后去优化不存在的问题。
    """
    for _ in range(8):
        _trace(db, "s-live", "success", source="live")
    for _ in range(20):
        _trace(db, "s-load", "tool_error", source="loadtest")
    for _ in range(10):
        _trace(db, "s-dev", "tool_error", source="dev")

    r = bm.business_report(db=db)
    assert r["turn_level"]["turns"] == 8                    # 只数 live
    assert r["turn_level"]["tool_error_rate"] == 0.0
    assert r["all_sources"]["turn_level"]["turns"] == 38    # 对照数全部


def test_distortion_is_reported_in_both_directions(db):
    """**不能只说"全量偏乐观"。** 真实库上实测到三个指标三个方向:压测失败把工具
    失败率抬高 4 倍,同时把转人工率压到三分之一。所以报表并排给两个数 + 差值。
    """
    for _ in range(6):
        _trace(db, "s1", "success", source="live")
    _trace(db, "s1", "handoff", source="live")
    for _ in range(20):
        _trace(db, "s2", "tool_error", source="loadtest")

    d = bm.business_report(db=db)["distortion"]["turn_level"]
    # 工具失败率:live 更低 → delta 为负
    assert d["tool_error_rate"]["delta"] < 0
    # 转人工率:live 更高 → delta 为正
    assert d["escalation_rate"]["delta"] > 0


def test_null_source_rows_are_treated_as_unknown(db):
    """历史行的 source 可能为 NULL(那批数据早于这个字段)。NULL 参与 IN 比较会被
    静默丢掉——用 COALESCE 归到 'unknown',于是它们进全量对照、不进 live 判定。"""
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO skill_traces (session_id, user_id, skill_name, tool_calls, "
            "outcome, created_at, source) VALUES (?,?,?,?,?,?,NULL)",
            ("s-old", "u1", "track-order", "[]", "success", _ts()))
        conn.commit()
    finally:
        conn.close()
    r = bm.business_report(db=db)
    assert r["turn_level"]["turns"] == 0                   # 不算进 live
    assert r["all_sources"]["turn_level"]["turns"] == 1    # 但算进全量


# --------------------------------------------- 轮级与会话级是两个口径

def test_turn_level_and_session_level_are_not_interchangeable(db):
    """每通会话 20 轮里 1 轮转人工:**轮级 5%、会话级 100%**。两个数都对,回答的是
    不同问题——运营关心的自助解决率是会话级的,而轮级 5% 看起来"几乎没转人工"。

    用 5 通会话而不是 1 通:会话级指标同样受 min_samples 约束,1 通会话算出的
    containment 是 0% 或 100%,那不是数据(这一点由
    `test_insufficient_metrics_are_nulled_not_dropped` 单独钉)。
    """
    for i in range(5):
        for _ in range(19):
            _trace(db, f"s{i}", "success")
        _trace(db, f"s{i}", "handoff")

    r = bm.business_report(db=db)
    assert r["turn_level"]["escalation_rate"] == pytest.approx(1 / 20)   # 5%
    assert r["session_level"]["session_escalation_rate"] == 1.0          # 100%
    assert r["session_level"]["containment_rate"] == 0.0


def test_containment_counts_sessions_without_any_handoff(db):
    for i in range(4):
        for _ in range(5):
            _trace(db, f"ok-{i}", "success")
    for _ in range(4):
        _trace(db, "bad", "success")
    _trace(db, "bad", "handoff")

    s = bm.business_report(db=db)["session_level"]
    assert s["sessions"] == 5
    assert s["sessions_escalated"] == 1
    assert s["containment_rate"] == 0.8
    assert s["avg_turns_per_session"] == pytest.approx(25 / 5)


# ----------------------------------------------------- 样本不足与除零

def test_insufficient_samples_yield_none_not_a_fabricated_rate(db):
    """1 条评价算出的差评率不是 0% 也不是 100%,是**没有数据**。凑出来的比率会被
    写进简历、被拿去汇报,而它什么都不代表。与门禁的 evaluable=False、异常扫描的
    service_insufficient 同一条纪律。"""
    _review(db, rating=5)
    r = bm.business_report(db=db)
    assert r["satisfaction"]["reviews"] == 1
    assert r["satisfaction"]["bad_review_rate"] is None
    scopes = [x["scope"] for x in r["insufficient"]]
    assert any("满意度" in s for s in scopes)


def test_sufficient_samples_do_yield_a_rate(db):
    for rating in (5, 5, 4, 2, 1):
        _review(db, rating=rating)
    r = bm.business_report(db=db)
    assert r["satisfaction"]["bad_review_rate"] == pytest.approx(2 / 5)
    assert not [x for x in r["insufficient"] if "满意度" in x["scope"]]


def test_insufficient_metrics_are_nulled_not_dropped(db):
    """**登记而不是删除**:报表里少一行与"这一行算不出来"在读者看来完全不同,
    前者会被当成"这个维度不存在"。"""
    _trace(db, "s1", "success")          # 1 轮 < 下限 5
    r = bm.business_report(db=db)
    assert "self_service_rate" in r["turn_level"]        # 键仍在
    assert r["turn_level"]["self_service_rate"] is None  # 值为 None
    assert r["turn_level"]["turns"] == 1                 # 原始计数仍如实给出


def test_empty_database_returns_none_not_zero(db):
    """0 和"没有数据"是两件事:前者是"一次都没发生过"(好消息),后者是"算不出来"。
    混成 0.0 会让空库看起来像满分。"""
    r = bm.business_report(db=db)
    assert r["turn_level"]["turns"] == 0
    assert r["turn_level"]["self_service_rate"] is None
    assert r["session_level"]["containment_rate"] is None


# --------------------------------------------------------------- 边界

def test_window_filters_by_time(db):
    for _ in range(6):
        _trace(db, "recent", "success", created_at=_ts(hours=-2))
    for _ in range(6):
        _trace(db, "old", "success", created_at=_ts(days=-30))
    assert bm.business_report(window_days=7, db=db)["turn_level"]["turns"] == 6
    assert bm.business_report(db=db)["turn_level"]["turns"] == 12


def test_bad_review_threshold_is_shared_not_redefined():
    """差评阈值与 reviews/anomaly_scan 共用同一常量,三处各写一遍必然漂移。"""
    from app.agent.tools.reviews import BAD_RATING_MAX
    assert bm.BAD_RATING_MAX is BAD_RATING_MAX


def test_decision_sources_match_the_watchdog(db):
    """业务报表与看门狗读同一处常量。哪天判定口径变了,两边一起变。"""
    from app.agent.runtime_context import DECISION_SOURCES
    assert bm.business_report(db=db)["decision_sources"] == sorted(DECISION_SOURCES)


def test_satisfaction_admits_it_cannot_filter_by_source(db):
    """`reviews` 表没有 source 列。如实标注这个边界,而不是假装它也过滤了。"""
    _review(db, rating=5)
    assert bm.business_report(db=db)["satisfaction"]["source_filtered"] is False


def test_cost_is_marked_unavailable_not_estimated(db):
    """本地无 token 计费表。估出来的成本会被当成实测报出去。"""
    cost = bm.business_report(db=db)["cost"]
    assert cost["available"] is False and cost["reason"]


def test_api_endpoint_returns_the_report(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.config import settings as st
    from app.db import Database as DB
    from app.db import set_db
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    monkeypatch.setattr(st.settings, "admin_token", "T")
    d = DB(db_path=str(tmp_path / "api.db"))
    d.init_schema()
    set_db(d)
    try:
        for _ in range(6):
            _trace(d, "s1", "success")
        from app.api.app import create_app
        body = TestClient(create_app()).get(
            "/api/eval/business", headers={"X-Admin-Token": "T"}).json()
        assert body["turn_level"]["turns"] == 6
        assert body["turn_level"]["self_service_rate"] == 1.0
        assert "distortion" in body and "insufficient" in body
    finally:
        set_db(None)
