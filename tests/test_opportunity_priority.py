"""商机优先级打分:确定性、可解释、可回退。

打分本身是纯函数(不读库不看时钟),所以大部分用例是直接喂 dict 断言分数关系;
只有 over-fetch 与开关回退这两条要过真库,因为它们要验的正是 SQL 那一层。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from app.agent.tools import growth
from app.agent.tools.priority import (conversion_rates, rank, score_opportunity)
from app.config.settings import settings
from app.db.database import Database


@pytest.fixture()
def db(monkeypatch):
    d = Database(db_path=os.path.join(tempfile.mkdtemp(), "prio.db"))
    d.init_schema()
    monkeypatch.setattr(growth, "get_db", lambda: d)
    from app.agent.tools import priority as prio_mod
    monkeypatch.setattr(prio_mod, "logger", prio_mod.logger)   # 占位,保持 import 被用到
    return d


def _unpaid(d, oid, user, hours, total):
    conn = d.connect()
    conn.execute(
        f"INSERT INTO orders (order_id,user,status,total,created_at) "
        f"VALUES (?,?,'unpaid',?,datetime('now','-{hours} hours'))", (oid, user, total))
    conn.commit()
    conn.close()


def _item(kind="unpaid_order", hours=48.0, amount=200.0):
    d = {"kind": kind, "stale_hours": hours}
    if amount is not None:
        d["amount"] = amount
    return d


# ---------- 纯打分:三项各自单调 ----------

def test_longer_stale_scores_higher():
    a, _ = score_opportunity(_item(hours=10), {})
    b, _ = score_opportunity(_item(hours=100), {})
    assert b > a


def test_bigger_amount_scores_higher():
    a, _ = score_opportunity(_item(amount=50), {})
    b, _ = score_opportunity(_item(amount=400), {})
    assert b > a


def test_higher_historical_conversion_scores_higher():
    low = {"unpaid_order": (0.1, 20)}
    high = {"unpaid_order": (0.9, 20)}
    assert score_opportunity(_item(), high)[0] > score_opportunity(_item(), low)[0]


def test_stale_saturates_so_zombie_rows_cannot_dominate():
    """滞留封顶:拖 3 个月和拖半年是同一档,否则最老的僵尸单永远霸榜。"""
    sat = settings.priority_stale_saturation_hours
    a, _ = score_opportunity(_item(hours=sat), {})
    b, _ = score_opportunity(_item(hours=sat * 10), {})
    assert a == b


def test_amount_saturates():
    cap = settings.priority_amount_cap
    a, _ = score_opportunity(_item(amount=cap), {})
    b, _ = score_opportunity(_item(amount=cap * 100), {})
    assert a == b


# ---------- 缺失与不足:两条容易写歪的边界 ----------

def test_missing_amount_uses_neutral_not_zero():
    """把"没有金额"当成"最差"是排序里最常见的一类偏见:咨询未下单这类商机
    本来就没有订单金额,按 0 打分等于系统性地永远排在最后。"""
    unknown, reason = score_opportunity(_item(amount=None), {})
    zero_ish, _ = score_opportunity(_item(amount=0.01), {})
    assert unknown > zero_ish
    assert "金额未知" in reason


def test_low_sample_conversion_falls_back_to_prior():
    """"1 单转 1 单 = 100%" 不能当真——与 anomaly_min_samples 同一条纪律。"""
    tiny = {"unpaid_order": (1.0, 1)}
    scored, reason = score_opportunity(_item(), tiny)
    prior_only, _ = score_opportunity(_item(), {})
    assert scored == prior_only
    assert "样本不足" in reason


def test_enough_samples_are_trusted():
    plenty = {"unpaid_order": (1.0, settings.priority_min_samples)}
    scored, reason = score_opportunity(_item(), plenty)
    assert scored > score_opportunity(_item(), {})[0]
    assert "历史转化" in reason


# ---------- 权重 ----------

def test_score_stays_in_unit_range_even_with_huge_weights(monkeypatch):
    """店主把某项权重调到 10,意图是"这项更重要",不是"总分溢出到 10 分"。"""
    monkeypatch.setattr(settings, "priority_weight_stale", 10.0)
    s, _ = score_opportunity(_item(hours=1e9, amount=1e9),
                             {"unpaid_order": (1.0, 100)})
    assert 0.0 <= s <= 1.0


def test_all_zero_weights_fall_back_to_equal_not_divide_by_zero(monkeypatch):
    for f in ("priority_weight_stale", "priority_weight_amount",
              "priority_weight_conversion"):
        monkeypatch.setattr(settings, f, 0.0)
    s, _ = score_opportunity(_item(), {})
    assert 0.0 <= s <= 1.0


# ---------- 排序 ----------

def test_rank_is_stable_for_equal_scores():
    """同分时按滞留时长兜底,保证顺序可复现——店主刷新两次看到的名单必须一样。"""
    items = [_item(hours=5, amount=None), _item(hours=50, amount=None)]
    out = rank(items, {})
    assert [i["stale_hours"] for i in out] == [50, 5]


def test_rank_attaches_reason_to_every_item():
    out = rank([_item(), _item(kind="abandoned_cart")], {})
    assert all(i["priority_reason"] and "priority_score" in i for i in out)


# ---------- 历史转化率读库 ----------

def test_conversion_rates_ignores_pending(db):
    conn = db.connect()
    for outcome in ("converted", "converted", "no_change", "pending", "pending"):
        conn.execute(
            "INSERT INTO outreach_drafts (opportunity_type,user_id,content,status,outcome,created_at) "
            "VALUES ('unpaid_order','u','c','sent',?,datetime('now'))", (outcome,))
    conn.commit()
    conn.close()
    rate, samples = conversion_rates(db)["unpaid_order"]
    # pending 既不算成功也不算失败:计进分母会系统性压低新上线的商机类型
    assert samples == 3 and abs(rate - 2 / 3) < 1e-9


# ---------- 端到端:排序真的改变了名单 ----------

def test_ranking_promotes_old_big_orders_over_recent_small_ones(db):
    _unpaid(db, "O-NEW", "小额新单", 26, 39)
    _unpaid(db, "O-OLD", "大额老单", 200, 1200)
    out = growth.find_opportunities(kind="unpaid_order", window_days=30, limit=10)
    assert out["ranked_by"] == "priority"
    assert [o["user_id"] for o in out["opportunities"]] == ["大额老单", "小额新单"]


def test_overfetch_lets_old_orders_reach_the_result(db):
    """关键:SQL 是 `ORDER BY created_at DESC LIMIT n`,取到的是**最新**的一批,
    而"最新"意味着滞留最短。只对 LIMIT 之后剩下的排序,真正该催的老单根本
    进不了候选池。超取就是为了让它进来。"""
    for i in range(6):
        _unpaid(db, f"O-NEW{i}", f"新单{i}", 26, 39)
    _unpaid(db, "O-OLD", "大额老单", 300, 2000)

    out = growth.find_opportunities(kind="unpaid_order", window_days=30, limit=3)
    assert len(out["opportunities"]) == 3
    assert out["opportunities"][0]["user_id"] == "大额老单"


def test_switch_off_restores_recency_order(db, monkeypatch):
    monkeypatch.setattr(settings, "opportunity_priority_enabled", False)
    _unpaid(db, "O-NEW", "小额新单", 26, 39)
    _unpaid(db, "O-OLD", "大额老单", 200, 1200)
    out = growth.find_opportunities(kind="unpaid_order", window_days=30, limit=10)
    assert out["ranked_by"] == "recency"
    assert [o["user_id"] for o in out["opportunities"]] == ["小额新单", "大额老单"]


def test_scoring_failure_falls_back_to_unranked_list(db, monkeypatch):
    """打分是锦上添花:它自己炸了也不能让"找商机"失败。"""
    import app.agent.tools.priority as prio

    def _boom(*a, **k):
        raise RuntimeError("scoring exploded")

    monkeypatch.setattr(prio, "conversion_rates", _boom)
    _unpaid(db, "O-1", "买家", 100, 500)
    out = growth.find_opportunities(kind="unpaid_order", window_days=30, limit=10)
    assert out["success"] is True and out["count"] == 1


def test_abandoned_cart_amount_comes_from_products(db):
    """弃单金额库里本来就有(carts.sku = products.product_id),之前没取,
    导致一车 2000 块和一车 29 块在打分时被当成同一档。"""
    conn = db.connect()
    conn.execute("INSERT INTO products (product_id,name,price,stock) VALUES ('P1','贵货',999,5)")
    conn.execute("INSERT INTO carts (user_id,sku,quantity,status,added_at) "
                 "VALUES ('买家','P1',2,'active',datetime('now','-72 hours'))")
    conn.commit()
    conn.close()
    out = growth.find_opportunities(kind="abandoned_cart", window_days=30, limit=10)
    assert out["opportunities"][0]["amount"] == pytest.approx(1998.0)


def test_unknown_sku_cart_is_kept_not_dropped(db):
    """LEFT JOIN 而不是 JOIN:外部来源的 sku 在本地 products 里可能没有对应行,
    那种情况宁可金额为空,也不能把整条弃单商机丢掉。"""
    conn = db.connect()
    conn.execute("INSERT INTO carts (user_id,sku,quantity,status,added_at) "
                 "VALUES ('买家','SKU-NOT-IN-PRODUCTS',1,'active',datetime('now','-72 hours'))")
    conn.commit()
    conn.close()
    out = growth.find_opportunities(kind="abandoned_cart", window_days=30, limit=10)
    assert out["count"] == 1
    assert "金额未知" in out["opportunities"][0]["priority_reason"]
