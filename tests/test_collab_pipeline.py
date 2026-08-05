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
    """run_once() 一次调用就打通 signal→diagnosis→drafts 全链路(见 run_once 文档字符串),
    不需要连续调两次去"分段推进"。"""
    _unpaid(db, "O1", "u1")
    _signal(db, corr="CHAIN")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="这款鞋已更新尺码建议,可以参考下"):
        collab.run_once()
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
    """刚转过人工的会话不该被拿去推销。单次 run_once() 即可验证:handle_signal
    从不为这类信号发布 insight.diagnosis,growth 段自然没有任何事件可消费。"""
    _unpaid(db, "O1", "u1")
    db.publish_event(bus.EV_SIGNAL_ANOMALY,
                     {"kind": "service_escalation", "subject": "s1", "session_id": "s1"},
                     bus.AGENT_SERVICE, bus.AGENT_ANALYST, "C9")
    with patch.object(collab, "_llm_explain", return_value="x"):
        collab.run_once()
    assert db.list_outreach_drafts() == []


def test_growth_creates_one_draft_per_opportunity(db):
    """单次 run_once() 已足以走完 signal→diagnosis→drafts 全链路。"""
    _unpaid(db, "O1", "u1")
    _unpaid(db, "O2", "u2")
    _signal(db, corr="C2")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="尺码建议已更新"):
        collab.run_once()
    assert len(db.list_outreach_drafts(status="draft")) == 2


def test_run_once_is_idempotent(db):
    """这里连续调两次是有意为之,验证的是"第二次没有剩余事件可消费"
    (claimed == 0),不是"每次调用推进一个阶段"——第一次调用已经把
    全链路跑完了(见 run_once 文档字符串)。"""
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
    assert stats["reclaimed"] == 0   # 开关关掉时回收也必须静默,不能悄悄改库


def test_worker_cycle_reclaims_stranded_events(db):
    """【I1】worker 崩在 claim 与 finish 之间留下的事件,必须能经 **worker 路径**
    自己回到队列。

    `reclaim_stale_events` 一直存在,但在这次修复前**只有测试调它**:
    `run_once()` 没调、CLI 的 cycle() 没调、启动路径也没调。于是
    `bus._finish_and_count` 的 docstring 里那句"交给 reclaim_stale_events 或人工
    决定"实际上只剩"人工"——被认领后遇上进程被杀的事件永久停在 processing。

    这条测试刻意**不直接调 db.reclaim_stale_events**(那样只会重新验证数据层,
    正是修复前就已经绿着的那部分),而是模拟崩溃后走 `collab.run_once()`,
    断言事件真的被本轮重新认领并处理掉。
    """
    _signal(db)
    # 模拟"认领后 worker 就挂了":claim 把行置成 processing,finish 永远没发生
    claimed = db.claim_events(bus.AGENT_ANALYST)
    assert len(claimed) == 1
    stranded_id = claimed[0]["id"]
    conn = db.connect()
    try:   # 把 consumed_at 拨老,越过回收阈值
        conn.execute("UPDATE agent_events SET consumed_at = datetime('now','-1 hours') "
                     "WHERE id = ?", (stranded_id,))
        conn.commit()
    finally:
        conn.close()
    assert [e for e in db.list_events() if e["id"] == stranded_id][0]["status"] == "processing"

    with patch.object(collab, "_llm_explain", return_value="尺码问题"):
        stats = collab.run_once()

    assert stats["reclaimed"] == 1
    assert stats["analyst"]["claimed"] == 1, "回收必须发生在消费之前,本轮就该被重新认领"
    assert [e for e in db.list_events() if e["id"] == stranded_id][0]["status"] == "done"


def test_fresh_processing_event_is_not_stolen_by_reclaim(db):
    """回收不得把**刚刚**被认领、还在正常处理中的事件抢回来(否则会重复处理)。"""
    _signal(db)
    claimed = db.claim_events(bus.AGENT_ANALYST)
    assert len(claimed) == 1
    stats = collab.run_once()          # consumed_at 是刚才,远没到 300 秒阈值
    assert stats["reclaimed"] == 0
    assert stats["analyst"]["claimed"] == 0


def test_two_insights_on_one_chain_do_not_double_queue_the_same_buyer(db):
    """【I3】一次扫描判定多个 SKU 异常 → 多条 insight.diagnosis → 同一批商机被
    重复起草,同一个买家在审批队列里躺着好几条几乎相同的草稿。

    人工闸挡得住"自动发出",挡不住审批队列被灌满——而店主是挨个批下去的,
    批完就等于给同一个买家连发了好几条。所以起草侧要跳过"该买家/该订单已经
    有一条待审草稿"的商机。
    """
    _unpaid(db, "O1", "u1")
    _signal(db, corr="C9", subject="P001")
    _signal(db, corr="C9", subject="P002")   # 同一条链上的第二个跨线商品

    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="尺码建议已更新"):
        collab.run_once()

    drafts = db.list_outreach_drafts(status="draft")
    assert len(drafts) == 1, f"同一买家同一订单被排了 {len(drafts)} 次队"
    assert drafts[0]["user_id"] == "u1" and drafts[0]["order_id"] == "O1"


def test_dedupe_key_is_per_order_not_per_buyer(db):
    """去重键是 (user_id, order_id):同一个买家名下**两笔不同**的滞留订单是两件
    真事,合并掉会让其中一笔永远得不到触达。"""
    _unpaid(db, "O1", "u1")
    _unpaid(db, "O2", "u1")   # 同一个买家,另一笔订单
    _signal(db, corr="C10")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="话术"):
        collab.run_once()
    drafts = db.list_outreach_drafts(status="draft")
    assert {d["order_id"] for d in drafts} == {"O1", "O2"}


def test_dedupe_ignores_already_reviewed_drafts(db):
    """只有**还在待审**的草稿参与去重:店主处理过的历史不该永久封杀再次触达。"""
    _unpaid(db, "O1", "u1")
    did = db.create_outreach_draft("stale_pending_order", "u1", "O1", "旧的", {},
                                   "r", "C0", "growth")
    db.review_outreach_draft(did, "rejected", "admin")   # 已驳回 → 不再挡新草稿
    _signal(db, corr="C11")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="新话术"):
        collab.run_once()
    assert len(db.list_outreach_drafts(status="draft")) == 1


def test_degraded_diagnosis_is_not_forwarded_to_marketing(db):
    """归因不可用的降级诊断不该被拿去写营销话术:_llm_draft 会把 conclusion
    原文嵌进买家消息,"仅列事实、归因暂不可用"这种半成品文案没有意义,
    重新扫一次的成本又很低——所以 handle_signal 的转发闸门里加了
    `not degraded` 这一条,这里把它钉死成回归测试。"""
    _unpaid(db, "O1", "u1")
    _signal(db, corr="C5", kind="refund_rate_high")
    with patch.object(collab, "_llm_explain", side_effect=RuntimeError("llm down")):
        stats = collab.run_once()
    assert stats["analyst"]["done"] == 1
    forwarded = [e for e in db.list_events() if e["event_type"] == bus.EV_INSIGHT_DIAGNOSIS]
    assert forwarded == []
    assert db.list_outreach_drafts() == []


def test_attach_correlation_failure_does_not_lose_drafts_or_fail_event(db, monkeypatch):
    """_attach_correlation 挂链失败(比如 UPDATE 抛异常)只是丢一个可追溯性标签,
    不该把已经成功创建的草稿"弄丢",也不该让整条 handle_insight 事件被判 failed
    (failed 的事件不会自动重试,会永久卡住)。"""
    _unpaid(db, "O1", "u1")
    _unpaid(db, "O2", "u2")
    _signal(db, corr="C3")

    real_get_db = collab.get_db
    calls = {"n": 0}

    class _FlakyDB:
        def connect(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated db failure on first attach")
            return real_get_db().connect()

    monkeypatch.setattr(collab, "get_db", lambda: _FlakyDB())

    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="尺码建议已更新"):
        stats = collab.run_once()

    assert stats["growth"]["failed"] == 0
    drafts = db.list_outreach_drafts(status="draft")
    assert len(drafts) == 2  # 两条草稿都还在,没有因为挂链失败而丢失
    corr_ids = {d["correlation_id"] for d in drafts}
    assert "C3" in corr_ids            # 第二条挂链成功
    assert any(c != "C3" for c in corr_ids)  # 第一条挂链失败,保留了 draft_outreach 自己生成的 corr


def test_attach_correlation_does_not_rewrite_reviewed_draft(db):
    """人工已经审批过的草稿,correlation_id 不该被再改写——状态守卫只对
    仍是 draft(待审)的行生效。"""
    _unpaid(db, "O1", "u1")
    _signal(db, corr="C4")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="尺码建议已更新"):
        collab.run_once()

    draft = db.list_outreach_drafts(status="draft")[0]
    assert db.review_outreach_draft(draft["id"], "approved", reviewed_by="owner1")

    changed = collab._attach_correlation(draft["id"], "SHOULD-NOT-APPLY")

    assert changed is False
    reviewed = db.get_outreach_draft(draft["id"])
    assert reviewed["status"] == "approved"
    assert reviewed["correlation_id"] != "SHOULD-NOT-APPLY"
