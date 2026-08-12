"""坐席工作队列的单位是"一个买家在等",不是"一次升级判定"。

**实测缺陷**(走查人工接管时抓到)。`/api/handoffs` 返回原始升级记录,坐席界面直接
渲染它,于是队列里 **50 条待办实际只来自 7 个会话**——其中一个会话占了 **37 条**
(跨度 08-02 到 08-12,intent 里 23 条是 return_request)。后果:

- **队列深度失真 7 倍**。坐席看到 50,以为有 50 个人在等;
- `resolve` 按 handoff_id 逐条,**要点 37 次才能清掉一个买家**;
- 处理完一条,同一个买家立刻又冒出来——看起来永远处理不完。

这与"324 条陈旧人工待办""30 条相同的假警报"是同一类:**计数单位错了 → 队列失去
意义 → 告警疲劳**。而 `SeatView` 的标题写的本来就是「待接管会话」——界面自己说的
单位就是会话。

**升级记录逐条保留不动**(审计轨迹,每次升级确实发生过),只改坐席从哪个视图工作。
"""

import pytest

from app.hitl.queue import HandoffQueue


@pytest.fixture()
def q(tmp_path):
    queue = HandoffQueue(db_path=str(tmp_path / "hitl.db"))
    queue.init_schema()
    return queue


def _add(q, session_id, intent="order_query", created_at=None):
    hid = q.add({"session_id": session_id, "intent": intent, "reasons": ["r"],
                 "user_input": "u", "reply": "a"})
    if created_at:
        conn = q.connect()
        try:
            conn.execute("UPDATE handoffs SET created_at=? WHERE handoff_id=?",
                         (created_at, hid))
            conn.commit()
        finally:
            conn.close()
    return hid


# --------------------------------------------------------------------------
# 聚合本身
# --------------------------------------------------------------------------

def test_one_row_per_buyer_not_per_escalation(q):
    """核心断言:同一会话升级 3 次,队列里只占 1 行。"""
    for i in range(3):
        _add(q, "s-1", created_at=f"2026-08-0{i+1} 10:00:00")
    _add(q, "s-2", created_at="2026-08-05 10:00:00")

    assert len(q.list_pending()) == 4, "原始记录必须逐条保留(审计轨迹)"
    sessions = q.list_pending_sessions()
    assert len(sessions) == 2, f"队列应是 2 个买家,拿到 {len(sessions)}"


def test_escalation_count_is_reported(q):
    """反复升级本身就是优先级信号,不能聚合掉就不见了。"""
    for i in range(3):
        _add(q, "s-1", created_at=f"2026-08-0{i+1} 10:00:00")
    g = q.list_pending_sessions()[0]
    assert g["escalations"] == 3
    assert len(g["handoff_ids"]) == 3


def test_waiting_since_is_the_earliest_not_the_latest(q):
    """**排队看最早那次升级。** 用最近一次会让一个等了 10 天、刚又问一句的买家
    排到最后——而他恰恰是最该先处理的。"""
    _add(q, "s-1", created_at="2026-08-02 08:00:00")
    _add(q, "s-1", created_at="2026-08-12 02:00:00")
    g = q.list_pending_sessions()[0]
    assert g["waiting_since"] == "2026-08-02 08:00:00"


def test_latest_shows_what_they_are_asking_now(q):
    """等待时长看最早,但"现在在问什么"要看最近一次。"""
    _add(q, "s-1", intent="order_query", created_at="2026-08-02 08:00:00")
    _add(q, "s-1", intent="complaint", created_at="2026-08-12 02:00:00")
    g = q.list_pending_sessions()[0]
    assert g["latest"]["intent"] == "complaint"


def test_longest_waiting_first(q):
    """等最久的排最前:这是坐席队列唯一合理的默认序。"""
    _add(q, "new", created_at="2026-08-11 10:00:00")
    _add(q, "old", created_at="2026-08-02 10:00:00")
    _add(q, "mid", created_at="2026-08-07 10:00:00")
    assert [g["session_id"] for g in q.list_pending_sessions()] == ["old", "mid", "new"]


def test_resolved_ones_are_excluded(q):
    hid = _add(q, "s-1")
    _add(q, "s-2")
    q.resolve(hid)
    assert [g["session_id"] for g in q.list_pending_sessions()] == ["s-2"]


def test_empty_queue(q):
    assert q.list_pending_sessions() == []
    assert q.count_pending_sessions() == 0


# --------------------------------------------------------------------------
# 一次清掉一个买家
# --------------------------------------------------------------------------

def test_resolve_session_clears_all_of_them(q):
    """逐条 resolve 要点 37 次(实测),而中间任何一次遗漏都会让这个会话重新出现。"""
    for i in range(5):
        _add(q, "s-1", created_at=f"2026-08-0{i+1} 10:00:00")
    _add(q, "s-2")

    assert q.resolve_session("s-1") == 5
    remaining = q.list_pending_sessions()
    assert [g["session_id"] for g in remaining] == ["s-2"]


def test_resolve_session_is_idempotent(q):
    """条件更新只对 pending 生效——与 `resolve` 同一套幂等纪律。"""
    _add(q, "s-1")
    assert q.resolve_session("s-1") == 1
    assert q.resolve_session("s-1") == 0


def test_resolve_session_does_not_touch_others(q):
    _add(q, "s-1")
    _add(q, "s-2")
    q.resolve_session("s-1")
    assert q.count_pending() == 1


# --------------------------------------------------------------------------
# 两个数必须分开报
# --------------------------------------------------------------------------

def test_buyer_count_and_escalation_count_are_different_numbers(q):
    """一个是排班依据(多少人在等),一个是质量信号(累计升级次数)。混成一个数
    正是这次要修的那个错——实测两者差 7 倍。"""
    for i in range(4):
        _add(q, "s-1", created_at=f"2026-08-0{i+1} 10:00:00")
    _add(q, "s-2")

    assert q.count_pending() == 5          # 升级次数
    assert q.count_pending_sessions() == 2  # 在等的买家数


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")
    monkeypatch.setattr(st.settings, "hitl_db_path", str(tmp_path / "hitl.db"))
    monkeypatch.setattr(st.settings, "db_path", str(tmp_path / "ecom.db"))
    from app.api.app import create_app
    return TestClient(create_app())


AUTH = {"X-Admin-Token": "T"}


def test_api_returns_sessions_and_both_counts(client):
    from app.db import get_db  # noqa: F401  (确保 app 已建好)
    import app.api.app as app_mod  # noqa: F401

    hitl_q = client.app.state.hitl.queue if hasattr(client.app.state, "hitl") else None
    if hitl_q is None:                       # hitl 未启用时端点仍要给出空结构
        d = client.get("/api/handoffs/sessions", headers=AUTH).json()
        assert d == {"sessions": [], "waiting_buyers": 0, "escalations_total": 0}
        return

    for i in range(3):
        _add(hitl_q, "s-1", created_at=f"2026-08-0{i+1} 10:00:00")
    _add(hitl_q, "s-2")

    d = client.get("/api/handoffs/sessions", headers=AUTH).json()
    assert d["waiting_buyers"] == 2
    assert d["escalations_total"] == 4
    assert len(d["sessions"]) == 2


def test_api_raw_endpoint_unchanged(client):
    """`/api/handoffs` 是审计视图,行为逐字节不变——不能为了界面好看改掉它。"""
    r = client.get("/api/handoffs", headers=AUTH)
    assert r.status_code == 200
    assert isinstance(r.json(), list)
