"""归因判不出来时,不能记成"触达失败"。

**实测缺陷**(走查协作链后半段时抓到)。库里 4 条已发送草稿,3 条的
`status_at_send`(归因基线)是空的——它们关联的 DEMO-006/008/010 在 orders 表里
不存在。而 `_progressed("", x)` 因为空串不在 STATUS_ORDER 里恒返回 False,于是这些
草稿**无论买家做什么都会被判 `no_change`**。

`no_change` 不是中性值:`priority.conversion_rates` 把
`outcome IN ('converted','no_change')` 当分母,所以一条**无法判断**的触达会被当成
一次**失败**的触达,永久拉低该商机类型的历史转化率——而那个转化率占商机打分权重
0.3(`settings.priority_weight_conversion`)。判不出来却被记成失败,是这个代码库
反复出现的同一类错(参见 anomaly 的 `service_insufficient`、service_quality 的
`other` 桶)。

改法很小,因为既有代码写得够防御:`conversion_rates` 的 WHERE 本来就是**白名单**,
新增第三种取值自动被排除在分母之外,那个函数一个字都不用改。
"""

import pytest

from app.scripts.attribute_outreach import OUTCOME_UNATTRIBUTABLE, _judge, attribute_once


@pytest.fixture()
def db(tmp_path):
    from app.db.database import Database
    d = Database(db_path=str(tmp_path / "attr.db"))
    d.init_schema()
    return d


def _sent_draft(d, order_id: str, baseline: str, kind: str = "stale_pending_order") -> int:
    """建一条已发送、待归因的草稿。"""
    did = d.create_outreach_draft(kind, "u1", order_id, "催一下", {}, "r", "C-1", "growth")
    d.review_outreach_draft(did, "approved", reviewed_by="admin")
    d.mark_outreach_sent(did)
    conn = d.connect()
    try:
        conn.execute("UPDATE outreach_drafts SET status_at_send = ?, "
                     "sent_at = datetime('now','-48 hours') WHERE id = ?",
                     (baseline, did))
        conn.commit()
    finally:
        conn.close()
    return did


def _order(d, order_id: str, status: str):
    conn = d.connect()
    try:
        conn.execute("INSERT INTO orders (order_id, \"user\", status, total, created_at) "
                     "VALUES (?, 'u1', ?, 100.0, datetime('now','-10 days'))",
                     (order_id, status))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 判定本身
# --------------------------------------------------------------------------

def test_missing_order_is_unattributable_not_a_failure(db):
    """订单查不到了(被删/被归档):没有可比对的当前状态。"""
    assert _judge({"order_id": "GONE", "status_at_send": "pending"}, db) \
        == OUTCOME_UNATTRIBUTABLE


def test_empty_baseline_is_unattributable(db):
    """实测那 3 条:基线为空,_progressed("", x) 恒 False → 永远 no_change。"""
    _order(db, "O-1", "shipped")
    for baseline in ("", "   ", None):
        assert _judge({"order_id": "O-1", "status_at_send": baseline}, db) \
            == OUTCOME_UNATTRIBUTABLE


def test_real_progression_still_converted(db):
    """正常路径不受影响:有基线、订单在、状态往前走了 → converted。"""
    _order(db, "O-2", "shipped")
    assert _judge({"order_id": "O-2", "status_at_send": "pending"}, db) == "converted"


def test_real_stagnation_still_no_change(db):
    """真的没动 → no_change。这一档必须保留,不能被"判不出来"吞掉。"""
    _order(db, "O-3", "pending")
    assert _judge({"order_id": "O-3", "status_at_send": "pending"}, db) == "no_change"


def test_sideways_move_is_no_change_not_unattributable(db):
    """退款这类侧向变化是"确定没有往前走",不是"判不出来"——两者别混。"""
    _order(db, "O-4", "refund_processing")
    assert _judge({"order_id": "O-4", "status_at_send": "pending"}, db) == "no_change"


# --------------------------------------------------------------------------
# 不进转化率分母(这是这次改动的全部意义)
# --------------------------------------------------------------------------

def test_unattributable_excluded_from_conversion_rates(db):
    """核心断言:判不出来的那条不该拉低转化率。

    造两条同类型草稿:一条真转化、一条判不出来。转化率必须是 100%(分母 1),
    而不是 50%(分母 2)。
    """
    from app.agent.tools.priority import conversion_rates

    _order(db, "OK-1", "shipped")
    _sent_draft(db, "OK-1", "pending")          # → converted
    _sent_draft(db, "GONE-1", "pending")        # → unattributable(订单不存在)

    stats = attribute_once(window_hours=1, db=db)
    assert stats["converted"] == 1
    assert stats[OUTCOME_UNATTRIBUTABLE] == 1
    assert stats["no_change"] == 0

    rates = conversion_rates(db)
    rate, samples = rates["stale_pending_order"]
    assert samples == 1, f"判不出来的那条不该进分母,分母应为 1,实际 {samples}"
    assert rate == pytest.approx(1.0), f"转化率应为 100%,实际 {rate:.0%}"


def test_unattributable_count_is_reported(db):
    """必须报出来:不进分母**又**不计数,等于这些草稿从所有统计里消失了——
    那与把它们记成失败是两种相反的错,都不可接受。"""
    _sent_draft(db, "GONE-2", "pending")
    stats = attribute_once(window_hours=1, db=db)
    assert stats["checked"] == 1
    assert stats[OUTCOME_UNATTRIBUTABLE] == 1


def test_unattributable_publishes_no_bus_event(db, monkeypatch):
    """不发总线事件:发一条 no_change 会让下游统计到一次并不存在的失败,
    而总线上也没有为"判不出来"设计的收件人。"""
    from app.multi_agent import bus
    published = []
    monkeypatch.setattr(bus, "publish",
                        lambda *a, **kw: published.append(a[0]) or 1)

    _sent_draft(db, "GONE-3", "pending")
    attribute_once(window_hours=1, db=db)
    assert published == [], f"判不出来却发了事件: {published}"


def test_normal_outcomes_still_publish(db, monkeypatch):
    """正常判定仍要回写总线——这次改动不能顺手把闭环掐掉。"""
    from app.multi_agent import bus
    published = []
    monkeypatch.setattr(bus, "publish",
                        lambda *a, **kw: published.append(a[0]) or 1)

    _order(db, "OK-2", "shipped")
    _sent_draft(db, "OK-2", "pending")
    attribute_once(window_hours=1, db=db)
    assert published == [bus.EV_OUTREACH_CONVERTED]


def test_still_idempotent(db):
    """幂等不变:第二次跑同一批,checked 为 0(条件更新抢不到)。"""
    _sent_draft(db, "GONE-4", "pending")
    first = attribute_once(window_hours=1, db=db)
    second = attribute_once(window_hours=1, db=db)
    assert first["checked"] == 1
    assert second["checked"] == 0
