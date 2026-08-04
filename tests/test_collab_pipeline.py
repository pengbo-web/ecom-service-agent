"""协作链路:信号→诊断→草稿的串联、correlation 贯通、LLM 失败降级、不给投诉用户推销。"""

from unittest.mock import patch

import pytest

from app.db.database import Database
from app.multi_agent import bus, collab, shared_context as sc


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    for mod in (bus, sc):
        monkeypatch.setattr(mod, "get_db", lambda: d)
    monkeypatch.setattr(collab, "get_db", lambda: d)
    from app.agent.tools import growth, shop_analytics
    monkeypatch.setattr(growth, "get_db", lambda: d)
    monkeypatch.setattr(shop_analytics, "get_db", lambda: d)
    return d


def _unpaid(d, oid, user, sku="P001"):
    """造一条"下单后久拖不发"的订单,供 growth.find_opportunities(kind="stale_pending_order") 命中。

    口径对齐 app/agent/tools/growth.py:该项目订单表没有"未付款"状态,
    状态恒为 pending;判定"久拖不发"靠 created_at 早于 48 小时前。
    这里下单时间设成 3 天前,落在窗口(14 天)内、又晚于 48 小时阈值。
    """
    conn = d.connect()
    try:
        conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                     "VALUES (?,?,'pending',199,datetime('now','-3 days'))", (oid, user))
        conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                     "VALUES (?, '跑鞋',?,1,199)", (oid, sku))
        conn.commit()
    finally:
        conn.close()


def _signal(d, corr="C1", kind="refund_rate_high", subject="P001"):
    return d.publish_event(bus.EV_SIGNAL_ANOMALY, {
        "kind": kind, "subject": subject, "subject_name": "跑鞋",
        "value": 0.3, "threshold": 0.15,
        "detail": {"orders": 20, "refunds": 6, "top_reason": "尺码不准"},
    }, bus.AGENT_SERVICE, bus.AGENT_ANALYST, corr)


def test_analyst_writes_diagnosis_and_forwards(db):
    _signal(db)
    with patch.object(collab, "_llm_explain", return_value="尺码标注不符,建议更新尺码表"):
        stats = collab.run_once()
    assert stats["analyst"]["done"] == 1
    entry = sc.fetch_entry(sc.KEY_DIAGNOSIS, "P001")
    assert entry["source_agent"] == bus.AGENT_ANALYST
    assert "尺码" in entry["value"]["conclusion"]
    forwarded = [e for e in db.list_events() if e["event_type"] == bus.EV_INSIGHT_DIAGNOSIS]
    assert len(forwarded) == 1
    assert forwarded[0]["target_agent"] == bus.AGENT_GROWTH


def test_correlation_id_runs_through_whole_chain(db):
    _unpaid(db, "O1", "u1")
    _signal(db, corr="CHAIN")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="这款鞋已更新尺码建议,可以参考下"):
        collab.run_once()      # analyst 段
        collab.run_once()      # growth 段
    chain = db.list_events(correlation_id="CHAIN")
    kinds = {e["event_type"] for e in chain}
    assert bus.EV_SIGNAL_ANOMALY in kinds
    assert bus.EV_INSIGHT_DIAGNOSIS in kinds
    assert bus.EV_DRAFTS_READY in kinds
    assert db.list_outreach_drafts(status="draft")[0]["correlation_id"] == "CHAIN"


def test_llm_failure_degrades_but_chain_continues(db):
    """归因用的 LLM 挂了,链路要继续:写一条"只有事实、没有归因"的诊断。"""
    _signal(db)
    with patch.object(collab, "_llm_explain", side_effect=RuntimeError("llm down")):
        stats = collab.run_once()
    assert stats["analyst"]["failed"] == 0        # 不算处理失败
    entry = sc.fetch(sc.KEY_DIAGNOSIS, "P001")
    assert entry["degraded"] is True
    assert entry["conclusion"]                    # 仍有可读文本(纯统计事实)


def test_escalation_signal_does_not_trigger_marketing(db):
    """刚转过人工的会话不该被拿去推销。"""
    _unpaid(db, "O1", "u1")
    db.publish_event(bus.EV_SIGNAL_ANOMALY,
                     {"kind": "service_escalation", "subject": "s1", "session_id": "s1"},
                     bus.AGENT_SERVICE, bus.AGENT_ANALYST, "C9")
    with patch.object(collab, "_llm_explain", return_value="x"):
        collab.run_once()
        collab.run_once()
    assert db.list_outreach_drafts() == []


def test_growth_creates_one_draft_per_opportunity(db):
    _unpaid(db, "O1", "u1")
    _unpaid(db, "O2", "u2")
    _signal(db, corr="C2")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="尺码建议已更新"):
        collab.run_once()
        collab.run_once()
    assert len(db.list_outreach_drafts(status="draft")) == 2


def test_run_once_is_idempotent(db):
    _signal(db)
    with patch.object(collab, "_llm_explain", return_value="x"):
        collab.run_once()
        second = collab.run_once()
    assert second["analyst"]["claimed"] == 0


def test_disabled_switch_is_a_no_op(db, monkeypatch):
    from app.config import settings as st
    _signal(db)
    monkeypatch.setattr(st.settings, "collab_enabled", False)
    stats = collab.run_once()
    assert stats["analyst"]["claimed"] == 0
