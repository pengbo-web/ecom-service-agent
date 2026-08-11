"""触达草稿不能引用与它无关的诊断。

**实测缺陷**(走查协作链时抓到)。造了一条真实异常:`HMDP-1`(Nike Air Max 270)
下单 5 笔退款 1 笔 → 退款率 20% 跨过 15% 告警线 → 参谋归因"实际尺码偏大,主因
『尺码不准,偏大一码』"。链路三跳全通,但产出的草稿是这样的:

    #43 → acc_buyer / ACC-P1（另一个商品）
          正文:「它**实际尺码偏大**，不少买家反馈建议选小一码哦～」
    #44 → acc_buyer / ACC-UNPAID（另一张订单，催发货）
          依据:「该款运动鞋实际尺码偏大…建议在详情页增加提示」

两条都是错的,而且是两种不同的错:

  #43 把 A 商品的结论**当成 B 商品的事实说给买家听了**——这是对客的事实性错误;
  #44 让人工审批看到一条与草稿内容毫不相干的依据——而店主正是靠那一栏决定批不批。

根因:`handle_insight` "每次都重新查同一份全局商机集合"(它自己的注释就这么写),
然后把诊断**无条件**喂给 `_llm_draft`、无条件当 `reason`。去重闸管住了数量,没有
任何东西管相关性。
"""

import pytest

from app.multi_agent.collab import (_diagnosis_applies_to, _diagnosis_line,
                                    _opportunity_reason)


DIAG_SKU = {
    "kind": "refund_rate_high", "subject": "HMDP-1",
    "subject_name": "Nike Air Max 270 运动鞋",
    "conclusion": "该款运动鞋实际尺码偏大，导致集中退货。建议详情页增加提示。",
}


# --------------------------------------------------------------------------
# 相关性判定
# --------------------------------------------------------------------------

def test_applies_when_opportunity_involves_the_subject_sku():
    assert _diagnosis_applies_to(DIAG_SKU, {"skus": ["HMDP-1"]}) is True
    assert _diagnosis_applies_to(DIAG_SKU, {"skus": ["OTHER", "HMDP-1"]}) is True


def test_does_not_apply_to_a_different_sku():
    """实测那条 #43:诊断讲 HMDP-1,草稿发给了 ACC-P1 的买家。"""
    assert _diagnosis_applies_to(DIAG_SKU, {"skus": ["ACC-P1"]}) is False


def test_unknown_skus_counts_as_not_applicable():
    """商机的 skus 为空 = "不知道涉及哪些 SKU",而"不知道"不能当"是"用。

    宁可少引用一条诊断(草稿退化成只按商机情境写,仍然正确),也不能对买家说
    一件关于别的商品的事。实测那条 #44 就是这种情况(它的商机压根没带 sku)。
    """
    assert _diagnosis_applies_to(DIAG_SKU, {}) is False
    assert _diagnosis_applies_to(DIAG_SKU, {"skus": []}) is False
    assert _diagnosis_applies_to(DIAG_SKU, {"skus": None}) is False


def test_sku_scoped_diagnosis_without_subject_does_not_apply():
    """SKU 级却没有 subject:判不出来就不用,不猜。"""
    assert _diagnosis_applies_to({"kind": "refund_rate_high"}, {"skus": ["X"]}) is False
    assert _diagnosis_applies_to({"kind": "refund_rate_high", "subject": "  "},
                                 {"skus": ["X"]}) is False


def test_shop_level_diagnosis_applies_to_everyone():
    """非 SKU 级的诊断(店铺级情绪异常之类)对所有买家都成立,不该被这道闸挡掉。"""
    shop = {"kind": "angry_rate_high", "subject": "shop",
            "conclusion": "近期激烈情绪占比偏高"}
    assert _diagnosis_applies_to(shop, {"skus": ["任意"]}) is True
    assert _diagnosis_applies_to(shop, {}) is True


def test_no_diagnosis_at_all_is_treated_as_applicable():
    """None/空 dict 走的是"没有诊断"这条路,由调用方决定传不传;这里不该报错。"""
    assert _diagnosis_applies_to(None, {"skus": ["X"]}) is True
    assert _diagnosis_applies_to({}, {"skus": ["X"]}) is True


# --------------------------------------------------------------------------
# prompt 里那一行
# --------------------------------------------------------------------------

def test_diagnosis_line_present_when_applicable():
    line = _diagnosis_line(DIAG_SKU)
    assert line.startswith("店铺诊断: ")
    assert "尺码偏大" in line
    assert line.endswith("\n")


def test_diagnosis_line_empty_not_the_string_none():
    """不适用时整行**不出现**。给一句"店铺诊断: None"会让模型以为有过诊断,
    然后去猜它的内容——那比不给更糟。"""
    for empty in (None, {}):
        line = _diagnosis_line(empty)
        assert line == ""
        assert "None" not in line


# --------------------------------------------------------------------------
# 人工审批看到的依据必须如实
# --------------------------------------------------------------------------

def test_opportunity_reason_describes_this_draft():
    """诊断不适用时,reason 必须讲"为什么有这条草稿",而不是照抄诊断结论。"""
    reason = _opportunity_reason({
        "situation_label": "下单后久未推进(已付款待发货)",
        "priority_reason": "滞留 212h · ¥899 · 历史转化样本不足(1)",
    })
    assert "下单后久未推进" in reason
    assert "滞留 212h" in reason
    assert "尺码" not in reason, "不能把不相干的诊断结论塞进依据"


def test_opportunity_reason_degrades_gracefully():
    """打分被关掉/失败时没有 priority_reason,退回商机类型说明;两者都没有则空串
    ——空依据也比误导性依据好。"""
    assert _opportunity_reason({"situation_label": "加购未下单"}) == "加购未下单"
    assert _opportunity_reason({"kind": "abandoned_cart"}) == "abandoned_cart"
    assert _opportunity_reason({}) == ""


# --------------------------------------------------------------------------
# 商机必须真的带出 sku(否则上面这道闸永远判"不适用")
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["stale_pending_order", "unpaid_order", "abandoned_cart"])
def test_autonomous_kinds_carry_skus(kind, tmp_path, monkeypatch):
    """自主起草的三类商机(AUTONOMOUS_DRAFT_KINDS)都必须带 skus。

    这一条钉的是"算了但没带过去"这个反复出现的错法:SQL 里 join 了 order_items,
    dict 里忘了带 sku,于是相关性闸永远判"不适用"——闸看起来在,实际上把所有诊断
    都挡掉了,而且是静默的(草稿照样产出,只是永远不引用诊断)。
    """
    from app.db.database import Database
    from app.agent.tools import growth

    db = Database(str(tmp_path / "ecom.db"))
    db.init_schema()
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO orders (order_id, \"user\", status, total, created_at) "
            "VALUES ('O-1', 'u1', ?, 899.0, datetime('now', '-10 days'))",
            ("pending" if kind == "stale_pending_order" else "unpaid",))
        conn.execute(
            "INSERT INTO order_items (order_id, sku, name, price, quantity) "
            "VALUES ('O-1', 'HMDP-1', 'Nike Air Max 270', 899.0, 1)")
        conn.execute(
            "INSERT INTO carts (user_id, sku, quantity, status, added_at) "
            "VALUES ('u1', 'HMDP-1', 1, 'active', datetime('now', '-10 days'))")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(growth, "get_db", lambda: db)
    out = growth.find_opportunities(kind=kind, window_days=14)
    assert out["success"], out
    assert out["opportunities"], f"{kind} 没造出商机,这条测试的前置数据要跟着改"
    for opp in out["opportunities"]:
        assert opp.get("skus"), f"{kind} 的商机没带 skus:相关性闸会永远判不适用"
        assert "HMDP-1" in opp["skus"]


def test_split_skus_dedupes_and_keeps_order():
    from app.agent.tools.growth import _split_skus

    assert _split_skus("A,B,A") == ["A", "B"]
    assert _split_skus(" A , B ") == ["A", "B"]
    assert _split_skus(None) == []
    assert _split_skus("") == []


# --------------------------------------------------------------------------
# 经营统计必须自报数据源(否则参谋会把渠道口径差异当成经营异常)
# --------------------------------------------------------------------------

def test_shop_overview_declares_its_data_scope(tmp_path, monkeypatch):
    """`shop_overview` 的返回值里必须带 `data_scope`。

    **这不是免责声明,是防一类具体的错误结论。** 实测:买家侧实际有 12 笔订单
    (经 `POST /api/order` 路由到 hmdp),而本工具读的 agent 订单库里只有 2 笔;
    参谋据此产出过一条诊断——「近 7 天仅 2 笔订单但产生 119 次客服对话,对话量
    远超订单量」。那是**数据源分裂的产物**,不是经营事实,但它以正常诊断的形式
    进了协作链、也会进店主的看板。

    参谋 Agent 只看得到工具返回的 JSON,口径不在里面它就无从得知——所以这一句
    必须在返回值里,不能只写在 docstring 或前端。
    """
    from app.db.database import Database
    from app.agent.tools import shop_analytics as sa

    db = Database(str(tmp_path / "ecom.db"))
    db.init_schema()
    monkeypatch.setattr(sa, "get_db", lambda: db)

    out = sa.shop_overview(window_days=7)
    scope = out.get("data_scope") or ""
    assert scope, "shop_overview 没有自报数据源"
    assert "hmdp" in scope, "口径里要点明缺的是哪个渠道,否则读的人不知道缺了什么"
    assert "没有同步" in scope
    # 直接钉住那句给模型的指令:它是为了挡住实测出现过的那条错误结论
    assert "对话量远超订单量" in scope
    assert "**" not in scope, "这一句同时给模型和界面用,不该带 markdown 星号"
