"""WS4 群体标签统计:确定性计数 + 样本不足置 None + 只报不回流。"""

import pytest

from app.agent.memory.profile import UserProfileStore
from app.agent.skills import cohort_stats
from app.db.database import Database


@pytest.fixture()
def env(tmp_path):
    db = Database(db_path=str(tmp_path / "cohort.db"))
    db.init_schema()
    store = UserProfileStore(str(tmp_path / "profile.db"))
    return db, store


def _trace(db, user_id, outcome, n=1):
    for _ in range(n):
        db.record_skill_trace("s", user_id, "track-order", [], outcome, source="live")


def test_rates_aggregated_per_tag(env):
    db, store = env
    for u in ("u1", "u2"):
        store.add_tag(u, "price_sensitive")
    _trace(db, "u1", "handoff", 3)
    _trace(db, "u1", "success", 3)
    _trace(db, "u2", "tool_error", 2)
    rows = cohort_stats.cohort_report(db=db, profile_store=store, min_samples=5)
    row = next(r for r in rows if r["tag"] == "price_sensitive")
    assert row["users"] == 2
    assert row["traces"] == 8
    assert row["handoff_rate"] == round(3 / 8, 3)
    assert row["tool_error_rate"] == round(2 / 8, 3)


def test_small_cohort_rates_are_none_not_zero(env):
    """1 单退 1 单是 100% 失败率?不,那是没数据——min_samples 之下置 None。"""
    db, store = env
    store.add_tag("u9", "newcomer")
    _trace(db, "u9", "tool_error", 2)
    rows = cohort_stats.cohort_report(db=db, profile_store=store, min_samples=5)
    row = next(r for r in rows if r["tag"] == "newcomer")
    assert row["traces"] == 2
    assert row["handoff_rate"] is None
    assert row["tool_error_rate"] is None


def test_users_without_traces_still_counted(env):
    db, store = env
    store.add_tag("u5", "silent")
    rows = cohort_stats.cohort_report(db=db, profile_store=store, min_samples=5)
    row = next(r for r in rows if r["tag"] == "silent")
    assert row["users"] == 1 and row["traces"] == 0
    assert row["handoff_rate"] is None


def test_report_is_read_only(env):
    db, store = env
    store.add_tag("u1", "t")
    _trace(db, "u1", "success", 6)
    before = sum(sum(v.values()) for v in db.skill_trace_counts().values())
    cohort_stats.cohort_report(db=db, profile_store=store)
    after = sum(sum(v.values()) for v in db.skill_trace_counts().values())
    assert after == before


def test_bad_tags_json_row_does_not_break_report(env):
    db, store = env
    store.add_tag("u1", "ok-tag")
    conn = store._conn
    conn.execute("INSERT OR REPLACE INTO user_profile (user_id, base_json, tags_json, updated_at) "
                 "VALUES ('u-bad', '', '{not-json', '')")
    conn.commit()
    rows = cohort_stats.cohort_report(db=db, profile_store=store, min_samples=5)
    assert any(r["tag"] == "ok-tag" for r in rows)
