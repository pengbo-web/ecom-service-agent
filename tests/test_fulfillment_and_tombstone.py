"""三件事的回归钉子:

1. **诊断相关性拆成两问。** SKU 相符只够做内容指导,当审批依据还要问题类型对得上
   ——`_diagnosis_applies_to`(讲的是不是这件商品)与
   `_diagnosis_explains_opportunity`(能不能解释这条商机为什么存在)。
2. **履约异常规则。** `stale_pending_order` 是产出草稿最多的商机类型,此前没有任何
   规则会为它发 `signal.anomaly`,于是它只能借一条退款率诊断当依据。
3. **无订阅者墓碑。** 状态机原本只能表达 done/failed,表达不了"合法地没往下走"。
"""

from datetime import datetime, timedelta

import pytest

from app.config.settings import settings
from app.db.database import Database


def _ts(**kw):
    return (datetime.now() + timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    from app.db import set_db
    set_db(d)
    monkeypatch.setattr(settings, "collab_enabled", True)
    yield d
    set_db(None)


def _order(d: Database, oid: str, sku: str, status: str, created_at: str,
           user: str = "u1", name: str = "Nike Air Max 270"):
    conn = d.connect()
    try:
        conn.execute("INSERT INTO orders (order_id, user, total, status, created_at) "
                     "VALUES (?,?,?,?,?)", (oid, user, 899.0, status, created_at))
        conn.execute("INSERT INTO order_items (order_id, name, sku, quantity, price) "
                     "VALUES (?,?,?,?,?)", (oid, name, sku, 1, 899.0))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------- ① 相关性:两问,不是一问

_OPP = {"kind": "stale_pending_order", "skus": ["HMDP-1"], "order_id": "O1",
        "user_id": "u1", "priority_reason": "滞留 120h · ¥899"}
_REFUND_DX = {"kind": "refund_rate_high", "subject": "HMDP-1",
              "conclusion": "该款跑鞋尺码偏大,导致 6 笔退款集中于尺码不准…"}
_FUL_DX = {"kind": "fulfillment_delay_high", "subject": "HMDP-1",
           "conclusion": "该商品已付款订单积压 5 单,最老一单滞留 120 小时…"}


def test_refund_diagnosis_informs_content_but_is_not_the_reason():
    """**实测缺陷 draft 41**:正文催发货,「依据」栏写的是尺码偏大导致退款。

    商品对得上(都是 HMDP-1),所以旧的单一布尔判了适用。但问题类型对不上——
    店主正是靠依据那一栏决定批不批。SKU 相符是必要条件,不是充分条件。
    """
    from app.multi_agent import collab
    assert collab._diagnosis_applies_to(_REFUND_DX, _OPP) is True
    assert collab._diagnosis_explains_opportunity(_REFUND_DX, _OPP) is False


def test_fulfillment_diagnosis_does_explain_a_stale_order():
    """履约延迟是**唯一**能解释滞留订单商机的诊断——这是加它的全部理由。"""
    from app.multi_agent import collab
    assert collab._diagnosis_explains_opportunity(_FUL_DX, _OPP) is True


def test_wrong_product_fails_both_questions():
    from app.multi_agent import collab
    other = dict(_OPP, skus=["HMDP-77"])
    assert collab._diagnosis_applies_to(_FUL_DX, other) is False
    assert collab._diagnosis_explains_opportunity(_FUL_DX, other) is False


def test_unregistered_opportunity_kind_is_not_explained():
    """白名单缺一条 → 少引用一条诊断(仍正确);默认适用 → 给审批人误导性依据。
    两者不对称,所以未登记判不适用。"""
    from app.multi_agent import collab
    assert collab._diagnosis_explains_opportunity(
        _FUL_DX, dict(_OPP, kind="unpaid_order")) is False


def test_opportunity_reason_is_used_as_the_fallback():
    """回落文案必须如实描述"为什么有这条草稿"。"""
    from app.multi_agent import collab
    assert "滞留 120h" in collab._opportunity_reason(_OPP)


# --------------------------------------------------------- ② 履约异常规则

def test_fulfillment_diagnostics_reuses_growth_stale_definition(db):
    """滞留口径**只能有一处**。两处各定一个小时数的后果是信号与草稿对"什么叫
    滞留"意见不一致:扫描说积压了,商机集合里一条都找不到,两边各自看都自洽。"""
    from app.agent.tools.growth import (_STALE_PENDING_HOURS,
                                        _STALE_PENDING_STATUS)
    from app.agent.tools.shop_analytics import fulfillment_diagnostics
    out = fulfillment_diagnostics(window_days=14)
    assert out["stale_after_hours"] == _STALE_PENDING_HOURS
    assert out["pending_status"] == _STALE_PENDING_STATUS


def test_only_paid_orders_count_as_fulfillment_problems(db):
    """未支付的单子不发货是买家的原因,不是履约问题。混进来这个指标就没意义了。"""
    from app.agent.tools.shop_analytics import fulfillment_diagnostics
    for i in range(6):
        _order(db, f"U{i}", "HMDP-1", "unpaid", _ts(hours=-120))
    p = fulfillment_diagnostics(window_days=14)["products"][0]
    assert p["orders"] == 6 and p["stale_orders"] == 0 and p["stale_rate"] == 0.0


def test_recent_pending_orders_are_not_stale_yet(db):
    """刚下单还没到阈值的不算积压——否则每一单在发货前都是"异常"。"""
    from app.agent.tools.shop_analytics import fulfillment_diagnostics
    for i in range(6):
        _order(db, f"R{i}", "HMDP-1", "pending", _ts(hours=-2))
    p = fulfillment_diagnostics(window_days=14)["products"][0]
    assert p["stale_orders"] == 0


def test_fulfillment_anomaly_is_reported(db):
    from app.agent.tools import anomaly
    for i in range(5):
        _order(db, f"S{i}", "HMDP-1", "pending", _ts(hours=-120))
    for i in range(3):
        _order(db, f"K{i}", "HMDP-1", "shipped", _ts(hours=-2))
    kinds = [a["kind"] for a in anomaly.anomaly_scan(window_days=14)["anomalies"]]
    assert "fulfillment_delay_high" in kinds


def test_fulfillment_anomaly_respects_min_samples(db):
    """1 单积压 1 单 = 100%,那不是异常,是没数据(与退款率同一条纪律)。"""
    from app.agent.tools import anomaly
    _order(db, "S0", "HMDP-1", "pending", _ts(hours=-120))
    kinds = [a["kind"] for a in anomaly.anomaly_scan(window_days=14)["anomalies"]]
    assert "fulfillment_delay_high" not in kinds


def test_fulfillment_anomaly_carries_the_stale_window_in_detail(db):
    """`stale_after_hours` 必须跟着 detail 走:同样的 0.62,按 48h 和按 4h 算是
    两件事,而看到告警的人(和模型)没有别的途径知道是哪一个。"""
    from app.agent.tools import anomaly
    for i in range(5):
        _order(db, f"S{i}", "HMDP-1", "pending", _ts(hours=-120))
    for i in range(3):
        _order(db, f"K{i}", "HMDP-1", "shipped", _ts(hours=-2))
    a = [x for x in anomaly.anomaly_scan(window_days=14)["anomalies"]
         if x["kind"] == "fulfillment_delay_high"][0]
    assert a["detail"]["stale_after_hours"] == 48
    assert a["detail"]["stale_orders"] == 5
    assert a["subject"] == "HMDP-1"


def test_fulfillment_anomaly_routes_to_both_analyst_and_guard():
    """subject 是 SKU → 走扇出;而且"发不出去的货就别再推广"本身就成立。"""
    from app.multi_agent import bus
    from app.multi_agent.routing import resolve
    targets = dict(resolve(bus.EV_SIGNAL_ANOMALY,
                           {"kind": "fulfillment_delay_high", "subject": "HMDP-1"}))
    assert set(targets) == {bus.AGENT_ANALYST, bus.AGENT_GUARD}


def test_fulfillment_diagnosis_wakes_marketing():
    """它进营销才有意义——否则滞留订单的草稿仍然只能借退款率诊断当依据。"""
    from app.multi_agent import bus
    from app.multi_agent.routing import resolve
    targets = dict(resolve(bus.EV_INSIGHT_DIAGNOSIS,
                           {"kind": "fulfillment_delay_high", "subject": "HMDP-1",
                            "degraded": False}))
    assert bus.AGENT_GROWTH in targets


def test_fulfillment_facts_pack_excludes_reviews():
    """差评讲的是商品本身,与发不出货无关。多给一个无关维度只会让归因把两件事
    混起来说。"""
    from app.multi_agent import collab
    facts = collab._FACTS_BY_KIND["fulfillment_delay_high"]
    assert "fulfillment_diagnostics" in facts
    assert "review_insights" not in facts


def test_fulfillment_buyer_hint_forbids_promising_a_date():
    """承诺守卫拦得住动作,拦不住话术(品牌语气那次实验实跑验证过)。所以提示
    本身就不能给模型任何"可以说个时间"的余地。"""
    from app.multi_agent.buyer_hints import hint_for
    hint = hint_for("fulfillment_delay_high")
    assert hint
    assert "不要给出任何具体的发货或到货时间" in hint
    # 与本表的既有纪律一致:不含数字、不含内部指标名
    assert "%" not in hint and "积压率" not in hint


# ------------------------------------------------------ ③ 无订阅者墓碑

def test_event_without_subscriber_leaves_a_tombstone(db):
    """实测形态:462 条 tool_error_rate_high 的诊断无人订阅,参谋**确实**归因了
    (共享上下文里有),但事件表一行都没有——"正当地走到尽头"与"根本没跑起来"
    在事件表上长得一模一样。"""
    from app.multi_agent import bus
    assert bus.publish(bus.EV_INSIGHT_DIAGNOSIS,
                       {"kind": "tool_error_rate_high", "subject": "track-order",
                        "conclusion": "失败率 84%…", "degraded": False},
                       bus.AGENT_ANALYST) is None      # 调用方语义不变
    rows = db.list_events(limit=10)
    assert len(rows) == 1
    assert rows[0]["status"] == bus.STATUS_NO_SUBSCRIBER
    assert rows[0]["target_agent"] == ""


def test_tombstone_is_invisible_to_every_workflow_query(db):
    """墓碑不是待办也不是故障。进了 claim 会变成一条永远没人处理的僵尸;
    进了 failed 会把"没人订阅"报成故障。"""
    from app.multi_agent import bus
    bus.publish(bus.EV_INSIGHT_DIAGNOSIS,
                {"kind": "tool_error_rate_high", "subject": "s",
                 "conclusion": "…", "degraded": False}, bus.AGENT_ANALYST)
    assert db.claim_events("analyst", limit=10) == []
    assert db.claim_events("growth", limit=10) == []
    assert db.claim_events("", limit=10) == []          # 空 target 也捞不到
    assert db.reclaim_stale_events(older_than_seconds=0) == 0
    assert db.count_failed_events() == 0
    assert db.list_failed_events() == []


def test_tombstone_is_visible_where_humans_look(db):
    from app.multi_agent import bus
    bus.publish(bus.EV_INSIGHT_DIAGNOSIS,
                {"kind": "tool_error_rate_high", "subject": "s",
                 "conclusion": "…", "degraded": False}, bus.AGENT_ANALYST)
    st = db.no_subscriber_stats()
    assert st["total"] == 1
    assert st["by_event_type"] == [{"event_type": "insight.diagnosis", "count": 1}]
    assert st["recent"][0]["payload"]["kind"] == "tool_error_rate_high"


def test_tombstone_stats_group_by_event_type(db):
    """总数只说"有一堆链断了";分组才指得出该给哪类事件加订阅者。"""
    from app.multi_agent import bus
    for k in ("tool_error_rate_high", "human_rate_high", "angry_rate_high"):
        bus.publish(bus.EV_INSIGHT_DIAGNOSIS,
                    {"kind": k, "subject": "s", "conclusion": "…",
                     "degraded": False}, bus.AGENT_ANALYST)
    bus.publish("signal.brand_new_type", {"kind": "k", "subject": "s"},
                bus.AGENT_SERVICE)
    st = db.no_subscriber_stats()
    assert st["total"] == 4
    assert dict((x["event_type"], x["count"]) for x in st["by_event_type"]) == {
        "insight.diagnosis": 3, "signal.brand_new_type": 1}


def test_subscribed_events_are_unaffected(db):
    """对照:有订阅者时照常落 pending、照常能被认领。"""
    from app.multi_agent import bus
    corr = bus.publish(bus.EV_SIGNAL_ANOMALY,
                       {"kind": "refund_rate_high", "subject": "HMDP-1"},
                       bus.AGENT_SERVICE)
    assert corr
    rows = db.list_events(correlation_id=corr)
    assert {r["status"] for r in rows} == {"pending"}
    assert len(db.claim_events("analyst", limit=10)) == 1
    assert len(db.claim_events("guard", limit=10)) == 1


def test_degraded_diagnosis_also_leaves_a_tombstone(db):
    """降级诊断不转营销(`_marketing_worthy` 判否)——那条链也该留痕,否则
    "归因降级了所以没往下走"这件事在事件表上同样不可见。"""
    from app.multi_agent import bus
    bus.publish(bus.EV_INSIGHT_DIAGNOSIS,
                {"kind": "refund_rate_high", "subject": "HMDP-1",
                 "conclusion": "仅列事实,归因暂不可用", "degraded": True},
                bus.AGENT_ANALYST)
    st = db.no_subscriber_stats()
    assert st["total"] == 1
    assert st["recent"][0]["payload"]["degraded"] is True
