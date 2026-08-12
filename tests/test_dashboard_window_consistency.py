"""看板的卡片与「最近请求」表格必须是同一个统计口径。

**实测缺陷**(走查可观测看板时抓到)。页面上方有窗口选择器(近1小时 / 近24小时 /
近7天 / 全部),卡片走 `/api/metrics?window_hours=N`,而紧接着的「最近请求」表格走
`/api/traces?limit=50` —— **不带窗口**。

选「近 24 小时」时:

    卡片:总请求数 40      页脚:统计口径 近 1 天
    表格:50 行,跨度 40.9 小时,其中落在近 1 天内的正好 40 行

选「近 1 小时」时更刺眼:

    卡片:总请求数 1       页脚:统计口径 近 1 小时
    表格:仍然 50 行、跨度 40.9 小时,其中落在近 1 小时内的只有 1 行

**窗口选择器对表格完全无效。** 危害不只是数字对不上:

- 排查时选「近 1 小时」看现状,点表格里某一行看调用链,实际看到的是 40 小时前的那次;
- 卡片说"错误率 0%"而表格里有一条老的失败行,读者会以为指标算错了。

而 `DashboardView.tsx` 里 `windowHours` 上方的注释早就把道理讲清楚了:

> 默认 24 小时而不是全部历史:全历史口径会让**已经修好的问题永远显示为红色**——
> 修完之后新调用全成功,累计值却被几百条旧失败压着。

同一条道理漏在了表格上。修法沿用 `all_traces(since=...)` 已有的参数形状。
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.observability.store import TraceStore
from app.observability.trace import Trace


NOW = time.time()


def _trace(tid, started_at, session_id="s1"):
    # 字段顺序:trace_id, session_id, user_input, intent, started_at, ended_at,
    # latency_ms, status, error, spans(见 app/observability/trace.py)
    return Trace(tid, session_id, "问一句", "order_query", started_at,
                 started_at + 0.1, 100, "ok", None, [])


@pytest.fixture()
def store(tmp_path):
    s = TraceStore(str(tmp_path / "tr.db"))
    s.init_schema()
    s.save_trace(_trace("fresh", NOW - 60))            # 1 分钟前
    s.save_trace(_trace("hours", NOW - 5 * 3600))      # 5 小时前
    s.save_trace(_trace("old", NOW - 40 * 3600))       # 40 小时前
    s.save_trace(_trace("other-session", NOW - 60, session_id="s2"))
    return s


# --------------------------------------------------------------------------
# store 层
# --------------------------------------------------------------------------

def test_since_filters_out_older_traces(store):
    ids = {r["trace_id"] for r in store.recent_traces(limit=50, since=NOW - 3600)}
    assert ids == {"fresh", "other-session"}, f"窗口没生效: {ids}"


def test_no_since_keeps_old_behavior(store):
    """不给 since = 全部历史,与改造前一致(默认不能变)。"""
    assert len(store.recent_traces(limit=50)) == 4


def test_since_composes_with_session_filter(store):
    """两个过滤要能叠加——原来是 if/else 两条独立 SQL,加窗口时最容易在这里出错。"""
    ids = {r["trace_id"] for r in
           store.recent_traces(limit=50, session_id="s1", since=NOW - 3600)}
    assert ids == {"fresh"}


def test_session_filter_alone_still_works(store):
    ids = {r["trace_id"] for r in store.recent_traces(limit=50, session_id="s1")}
    assert ids == {"fresh", "hours", "old"}


def test_ordering_still_newest_first(store):
    rows = store.recent_traces(limit=50)
    assert [r["trace_id"] for r in rows][:2] == ["fresh", "other-session"] or \
           [r["trace_id"] for r in rows][:2] == ["other-session", "fresh"]
    assert rows[-1]["trace_id"] == "old", "排序被改坏了"


def test_limit_still_applies(store):
    assert len(store.recent_traces(limit=2)) == 2


# --------------------------------------------------------------------------
# 端点层:与 /api/metrics 同名同义
# --------------------------------------------------------------------------

@pytest.fixture()
def client(store, monkeypatch, tmp_path):
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "")
    monkeypatch.setattr(st.settings, "auth_enabled", False)
    from app.api.app import create_app
    from app.api.session_manager import SessionManager
    mgr = SessionManager(agent_factory=lambda p, u=None: None)
    return TestClient(create_app(session_manager=mgr, trace_store=store, hitl=None))


def test_endpoint_respects_window(client):
    """**核心断言**:同一个 window_hours,表格与卡片看到的是同一批 trace。"""
    rows = client.get("/api/traces?limit=50&window_hours=1").json()
    assert {r["trace_id"] for r in rows} == {"fresh", "other-session"}


def test_endpoint_window_zero_is_all_history(client):
    """`window_hours=0` = 全部历史,与 /api/metrics 的语义一致,也是改造前的默认。"""
    assert len(client.get("/api/traces?limit=50&window_hours=0").json()) == 4
    assert len(client.get("/api/traces?limit=50").json()) == 4


def test_endpoint_matches_metrics_total(client):
    """卡片的「总请求数」与表格行数在同一窗口下必须对得上。

    这正是走查时对不上的那一处:卡片 1,表格 50。
    """
    for hours in (1, 24, 24 * 7):
        m = client.get(f"/api/metrics?window_hours={hours}").json()
        rows = client.get(f"/api/traces?limit=50&window_hours={hours}").json()
        total = m["total_traces"]
        assert total == len(rows), (
            f"window={hours}h 时卡片 {total} 条、表格 {len(rows)} 行,口径又分叉了")
