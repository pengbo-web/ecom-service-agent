"""触达仲裁的另两条轴:**频次下限**(买家级)与**推广静默**(商品级)。

既有 `test_arbitration.py` 覆盖的是"状态类"那一轴(接管中/未结工单)。这里补的
两条与它并列,合起来才是完整的"别在错的时候打扰错的人":

    店铺级(工单数)  → routing._marketing_paused,fail-open
    买家级(状态+频次) → arbitration,**fail-closed**   ← 本文件
    商品级(该商品在出问题) → arbitration,**fail-closed**  ← 本文件

外加 `signal.anomaly` 的**扇出**(参谋 + 风控两个订阅者)——那是这个仓库路由表
里第一处真正的一对多,之前 6 个事件 6 条订阅全是 1:1。
"""

from datetime import datetime, timedelta

import pytest

from app.config.settings import settings
from app.db.database import Database
from app.multi_agent import arbitration as arb
from app.multi_agent import bus
from app.multi_agent.routing import P_URGENT, _pause_worthy, resolve


def _ts(**delta) -> str:
    return (datetime.now() + timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    d.create_conversation("u1")
    return d


@pytest.fixture()
def interval_on(monkeypatch):
    """打开频次下限。conftest 全局把它钉成 0,专项用例自行开启。"""
    monkeypatch.setattr(settings, "outreach_min_interval_hours", 1.0)


def _sent_draft(d: Database, user="u1", order="O1", sent_at=None) -> int:
    """造一条**已投递**的草稿。频次规则只认 status='sent'。"""
    did = d.create_outreach_draft("unpaid_order", user, order, "催一下", {},
                                  "", "C1", "growth")
    conn = d.connect()
    try:
        conn.execute("UPDATE outreach_drafts SET status='sent', sent_at=? WHERE id=?",
                     (sent_at or _ts(), did))
        conn.commit()
    finally:
        conn.close()
    return did


# ---------------------------------------------------------------- 频次下限

def test_disabled_interval_never_blocks(db):
    """阈值为 0 = 关。刚发过也照样放行——机制在、策略由部署方开。"""
    _sent_draft(db)
    ok, code, _ = arb.check_outreach_allowed("u1", hitl=None, db=db)
    assert ok is True and code == ""


def test_blocks_when_last_outreach_is_too_recent(db, interval_on):
    _sent_draft(db, sent_at=_ts(minutes=-10))
    ok, code, reason = arb.check_outreach_allowed("u1", hitl=None, db=db)
    assert ok is False
    assert code == arb.BLOCK_TOO_SOON
    assert "最小间隔" in reason


def test_allows_when_interval_has_passed(db, interval_on):
    _sent_draft(db, sent_at=_ts(hours=-3))
    ok, code, _ = arb.check_outreach_allowed("u1", hitl=None, db=db)
    assert ok is True and code == ""


def test_allows_buyer_who_never_received_outreach(db, interval_on):
    ok, code, _ = arb.check_outreach_allowed("u1", hitl=None, db=db)
    assert ok is True and code == ""


def test_only_sent_drafts_count_towards_the_interval(db, interval_on):
    """draft/approved 不算"刚被打扰过"。

    approved 的草稿可能投递失败后被退回 draft(`revert_outreach_to_pending`),
    把它算进来会让**一次失败的投递**白白冻结这个买家一小时。
    """
    db.create_outreach_draft("unpaid_order", "u1", "O9", "还没发", {}, "", "C9", "growth")
    ok, code, _ = arb.check_outreach_allowed("u1", hitl=None, db=db)
    assert ok is True and code == ""


def test_interval_rule_applies_even_without_hitl(db, interval_on):
    """回归钉子:频次规则**与 HITL 无关**,不能被 `hitl is None` 那条早返回短路。

    改造前 `hitl is None` 是函数体第一件事就 return True——把频次判在它后面等于
    "没配人工坐席的部署上这条规则静默失效",而它防的是骚扰,跟有没有坐席无关。
    """
    _sent_draft(db, sent_at=_ts(minutes=-1))
    ok, code, _ = arb.check_outreach_allowed("u1", hitl=None, db=db)
    assert ok is False and code == arb.BLOCK_TOO_SOON


def test_interval_query_failure_fails_closed(db, interval_on, monkeypatch):
    """查不到上次时间 ≠ 没发过。库读不了时两者无法区分,按不可发处理。"""
    def boom(_uid):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "last_outreach_sent_at", boom)
    ok, code, _ = arb.check_outreach_allowed("u1", hitl=None, db=db)
    assert ok is False and code == arb.BLOCK_UNKNOWN


def test_last_outreach_sent_at_is_per_user(db):
    _sent_draft(db, user="u1", sent_at="2026-01-01 00:00:00")
    assert db.last_outreach_sent_at("u1") == "2026-01-01 00:00:00"
    assert db.last_outreach_sent_at("u2") is None
    assert db.last_outreach_sent_at("") is None


def test_last_outreach_sent_at_takes_the_latest(db):
    _sent_draft(db, order="O1", sent_at="2026-01-01 00:00:00")
    _sent_draft(db, order="O2", sent_at="2026-03-01 00:00:00")
    _sent_draft(db, order="O3", sent_at="2026-02-01 00:00:00")
    assert db.last_outreach_sent_at("u1") == "2026-03-01 00:00:00"


# ------------------------------------------------------------ 商品级静默

def _order_with_sku(d: Database, order_id: str, sku: str, user="u1"):
    conn = d.connect()
    try:
        conn.execute("INSERT INTO orders (order_id, user, total, status, created_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (order_id, user, 899.0, "unpaid", _ts()))
        conn.execute("INSERT INTO order_items (order_id, name, sku, quantity, price) "
                     "VALUES (?, ?, ?, ?, ?)", (order_id, "跑鞋", sku, 1, 899.0))
        conn.commit()
    finally:
        conn.close()


def test_pause_blocks_draft_for_that_product(db):
    _order_with_sku(db, "O1", "HMDP-1")
    db.add_promotion_pause("HMDP-1", until=_ts(hours=6),
                           kind="refund_rate_high", reason="退款率异常")
    draft = {"user_id": "u1", "order_id": "O1"}
    ok, code, reason = arb.check_outreach_allowed("u1", hitl=None, db=db, draft=draft)
    assert ok is False
    assert code == arb.BLOCK_PROMOTION_PAUSED
    assert "静默" in reason


def test_pause_does_not_block_a_different_product(db):
    _order_with_sku(db, "O2", "HMDP-9")
    db.add_promotion_pause("HMDP-1", until=_ts(hours=6))
    ok, code, _ = arb.check_outreach_allowed(
        "u1", hitl=None, db=db, draft={"user_id": "u1", "order_id": "O2"})
    assert ok is True and code == ""


def test_expired_pause_does_not_block(db):
    _order_with_sku(db, "O1", "HMDP-1")
    db.add_promotion_pause("HMDP-1", until=_ts(hours=-1))
    ok, code, _ = arb.check_outreach_allowed(
        "u1", hitl=None, db=db, draft={"user_id": "u1", "order_id": "O1"})
    assert ok is True and code == ""


def test_pause_matches_across_item_id_formats(db):
    """SKU 比较走 `product_ref.same_item` 归一,不是裸字符串相等。

    这个仓库在 `render_buyer_hints` 上栽过一次:诊断 subject 是
    `order_items.sku`(`HMDP-1`),对面是 hmdp product.id(`1`),裸相等永远不成立,
    而且**不报错、只是永远匹配不上**。静默这条闸不能重犯——它拦不住的后果是一个
    正在出问题的商品继续被推广。
    """
    _order_with_sku(db, "O1", "HMDP-1")
    db.add_promotion_pause("1", until=_ts(hours=6))     # 另一种格式的同一件商品
    ok, code, _ = arb.check_outreach_allowed(
        "u1", hitl=None, db=db, draft={"user_id": "u1", "order_id": "O1"})
    assert ok is False and code == arb.BLOCK_PROMOTION_PAUSED


def test_pause_rule_is_skipped_without_a_draft(db):
    """跟进序列拿不到草稿(它手上是 outreach_followups 行,没有商品维度)。

    **不适用 ≠ 拒绝**:判成拒绝会让跟进序列被一条与它无关的规则全部掐死。
    """
    db.add_promotion_pause("HMDP-1", until=_ts(hours=6))
    ok, code, _ = arb.check_outreach_allowed("u1", hitl=None, db=db)
    assert ok is True and code == ""


def test_pause_rule_is_skipped_for_drafts_without_an_order(db):
    """弃单/咨询未下单的商机 order_id 恒为空串,没有商品维度可判。"""
    db.add_promotion_pause("HMDP-1", until=_ts(hours=6))
    ok, code, _ = arb.check_outreach_allowed(
        "u1", hitl=None, db=db, draft={"user_id": "u1", "order_id": ""})
    assert ok is True and code == ""


def test_pause_query_failure_fails_closed(db, monkeypatch):
    def boom(now=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "active_promotion_pauses", boom)
    ok, code, _ = arb.check_outreach_allowed(
        "u1", hitl=None, db=db, draft={"user_id": "u1", "order_id": "O1"})
    assert ok is False and code == arb.BLOCK_UNKNOWN


def test_active_promotion_pauses_filters_by_time_only(db):
    """SQL 只判"还没过期",商品比较留给调用方归一后做(见方法 docstring)。"""
    db.add_promotion_pause("HMDP-1", until=_ts(hours=6))
    db.add_promotion_pause("HMDP-2", until=_ts(hours=-6))
    rows = db.active_promotion_pauses()
    assert [r["subject"] for r in rows] == ["HMDP-1"]


def test_repeated_pauses_keep_both_rows(db):
    """不去重:重复静默是幂等的,而合并会丢掉"这次是哪条链要求的"这个线索。"""
    db.add_promotion_pause("HMDP-1", until=_ts(hours=2), correlation_id="SCAN-A")
    db.add_promotion_pause("HMDP-1", until=_ts(hours=8), correlation_id="SCAN-B")
    rows = db.active_promotion_pauses()
    assert len(rows) == 2
    assert rows[0]["correlation_id"] == "SCAN-B"      # 晚到期的在前


# ------------------------------------------------------- signal.anomaly 扇出

@pytest.mark.parametrize("kind", ["refund_rate_high", "bad_review_rate_high"])
def test_sku_scoped_anomaly_fans_out_to_two_agents(kind):
    """本仓库路由表里第一处真正的一对多。"""
    targets = dict(resolve(bus.EV_SIGNAL_ANOMALY,
                           {"kind": kind, "subject": "HMDP-1"}))
    assert set(targets) == {bus.AGENT_ANALYST, bus.AGENT_GUARD}


@pytest.mark.parametrize("kind,subject", [
    ("tool_error_rate_high", "track-order"),   # subject 是 skill 名
    ("human_rate_high", "track-order"),        # 同上
    ("service_escalation", "c-abc123"),        # subject 是会话 id
    ("angry_rate_high", "shop"),               # subject 是店铺级常量
])
def test_non_product_anomalies_do_not_reach_guard(kind, subject):
    """静默的 subject 要跟 `order_items.sku` 比较。放错一个 kind,这条闸就变成
    永远不触发的死代码——不报错、不留日志。"""
    targets = dict(resolve(bus.EV_SIGNAL_ANOMALY,
                           {"kind": kind, "subject": subject}))
    assert bus.AGENT_GUARD not in targets
    assert bus.AGENT_ANALYST in targets


def test_anomaly_without_subject_does_not_reach_guard():
    """拿不到商品就无从静默。"""
    assert not _pause_worthy({"kind": "refund_rate_high", "subject": "  "})


def test_guard_outranks_analyst_on_the_same_event():
    """风控要**更早**落地,不是更重要:归因几十秒里可能有人点批准发出推广。"""
    targets = dict(resolve(bus.EV_SIGNAL_ANOMALY,
                           {"kind": "refund_rate_high", "subject": "HMDP-1"}))
    assert targets[bus.AGENT_GUARD] == P_URGENT
    assert targets[bus.AGENT_GUARD] > targets[bus.AGENT_ANALYST]


def test_pause_worthy_is_pure(monkeypatch):
    """路由谓词跑在买家会话热路径上:不许读配置(配置会让路由结果随时间漂移)。"""
    monkeypatch.setattr(settings, "collab_promotion_pause_hours", 0)
    assert _pause_worthy({"kind": "refund_rate_high", "subject": "HMDP-1"}) is True
    monkeypatch.setattr(settings, "collab_promotion_pause_hours", 24)
    assert _pause_worthy({"kind": "refund_rate_high", "subject": "HMDP-1"}) is True


# ------------------------------------------------------------ handle_guard

def test_handle_guard_writes_a_pause(db, monkeypatch):
    from app.multi_agent import collab
    monkeypatch.setattr(settings, "collab_promotion_pause_hours", 24)
    monkeypatch.setattr("app.db.get_db", lambda: db)
    out = collab.handle_guard({"payload": {"kind": "refund_rate_high",
                                           "subject": "HMDP-1",
                                           "subject_name": "Nike 跑鞋"},
                               "correlation_id": "SCAN-X"})
    assert out["paused"] is True
    rows = db.active_promotion_pauses()
    assert len(rows) == 1
    assert rows[0]["subject"] == "HMDP-1"
    assert rows[0]["correlation_id"] == "SCAN-X"
    assert "Nike 跑鞋" in rows[0]["reason"]


def test_handle_guard_still_consumes_when_policy_is_off(db, monkeypatch):
    """关掉时**不是不消费,而是不写记录**——扇出跑通了这件事的可观测性不该
    依赖某个部署方是否采纳了这条策略。"""
    from app.multi_agent import collab
    monkeypatch.setattr(settings, "collab_promotion_pause_hours", 0)
    monkeypatch.setattr("app.db.get_db", lambda: db)
    out = collab.handle_guard({"payload": {"kind": "refund_rate_high",
                                           "subject": "HMDP-1"},
                               "correlation_id": "SCAN-X"})
    assert out["paused"] is False
    assert db.active_promotion_pauses() == []


def test_handle_guard_raises_on_write_failure(db, monkeypatch):
    """与参谋侧的店主通知相反,这里**不吞异常**:通知写失败只是少一条提醒,
    静默写失败意味着一个正在出问题的商品仍然可以被推广。抛出去让事件判 failed,
    `retry_failed_event` 才能把它捞回来。"""
    from app.multi_agent import collab
    monkeypatch.setattr(settings, "collab_promotion_pause_hours", 24)

    def boom(**_kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "add_promotion_pause", boom)
    monkeypatch.setattr("app.db.get_db", lambda: db)
    with pytest.raises(RuntimeError):
        collab.handle_guard({"payload": {"kind": "refund_rate_high",
                                         "subject": "HMDP-1"}})


# ------------------------------------------------------ API 层:拒绝要透出去

@pytest.fixture()
def api(monkeypatch, tmp_path):
    """隔离库 + 真实 app,验证拒绝确实走到 HTTP 响应上。"""
    from fastapi.testclient import TestClient

    from app.config import settings as st
    from app.db import Database, set_db
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    monkeypatch.setattr(st.settings, "admin_token", "T")
    d = Database(db_path=str(tmp_path / "api.db"))
    d.init_schema()
    set_db(d)
    from app.api.app import create_app
    yield TestClient(create_app()), d
    set_db(None)


def test_promotion_pause_surfaces_in_the_approve_response(api, monkeypatch):
    """前端靠 `sent=false` + `reason` 把草稿留在队列并亮出原因(见
    GrowthPanel.onApprove),所以这两个字段是契约,不能只在内部返回值里正确。"""
    client, d = api
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda _d: True)
    _order_with_sku(d, "O1", "HMDP-1", user="u1")
    d.add_promotion_pause("HMDP-1", until=_ts(hours=6),
                          kind="refund_rate_high", reason="退款率异常")
    did = d.create_outreach_draft("unpaid_order", "u1", "O1", "催一下", {},
                                  "", "C1", "growth")
    body = client.post(f"/api/admin/growth/drafts/{did}/approve",
                       headers={"X-Admin-Token": "T"}).json()
    assert body["sent"] is False
    assert body["block_code"] == arb.BLOCK_PROMOTION_PAUSED
    assert "静默" in body["reason"]
    # 被拦下的草稿必须**留在待审队列**,不能被消耗掉那次幂等机会
    assert d.get_outreach_draft(did)["status"] == "draft"


def test_rate_limit_surfaces_in_the_approve_response(api, monkeypatch, interval_on):
    client, d = api
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda _d: True)
    _sent_draft(d, user="u1", order="O0", sent_at=_ts(minutes=-5))
    did = d.create_outreach_draft("unpaid_order", "u1", "O1", "再催一次", {},
                                  "", "C2", "growth")
    body = client.post(f"/api/admin/growth/drafts/{did}/approve",
                       headers={"X-Admin-Token": "T"}).json()
    assert body["sent"] is False
    assert body["block_code"] == arb.BLOCK_TOO_SOON
    assert d.get_outreach_draft(did)["status"] == "draft"


def test_run_once_consumes_guard_before_analyst(monkeypatch):
    """顺序钉子:风控排在参谋后面就失去意义(归因几十秒里推广可能已被批准发出)。

    断言**实际调用顺序**,不读源码文本。原版比的是 `getsource` 里三个名字出现的
    位置,而 docstring 里为解释这个顺序而提到它们,恰好就会让断言看的是注释而不是
    代码——它红过一次,而行为完全正确。
    """
    from app.multi_agent import bus, collab

    consumed: list[str] = []
    monkeypatch.setattr(bus, "consume",
                        lambda target, handler, limit=20: consumed.append(target)
                        or {"claimed": 0, "done": 0, "failed": 0})
    monkeypatch.setattr(collab, "_reclaim_stale", lambda *_a, **_k: 0)
    collab.run_once()
    assert consumed == [bus.AGENT_GUARD, bus.AGENT_ANALYST, bus.AGENT_GROWTH]
