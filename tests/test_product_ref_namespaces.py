"""同一件商品在库里有两种写法,比较必须归一。

**实测到的静默失效**(走查 buyer_hints 时抓到)。同一份 ecom.db:

    order_items.sku      'HMDP-1'、'ACC-P1'、'SHOE-270-BK-42' …
    carts.sku            '1'、'2'、'ACC-P1'
    products.product_id  'ACC-P1'、'SHOE-270-BK-42' …（没有 'HMDP-1'，也没有 '1'）

`HMDP-{id}` 是 `POST /api/order` 为 hmdp 渠道商品造的本地 sku,而购物车那条路直接
把裸 `item_id` 当 sku 存。前端两处传的都是裸 id。

已确证两处失效,都是**静默**的(不报错、不留日志):

1. 买家侧应答提示对每一个 hmdp 商品都永不注入——诊断 subject 是 `HMDP-1`,
   前端传的 `current_item_id` 是 `1`。实测 `render_buyer_hints(entries, "1")` 返回
   空串,整个"跨 Agent 经验回流"的商品级通路是断的。
2. 诊断相关性闸对弃单商机永远判"不适用"——subject `HMDP-1` vs 商机 sku `1`。

本组测试钉住归一逻辑本身 + 两个消费方的接线,并且**同时钉住"不能过度归一"**:
把两个不同商品判成同一个,后果比不归一更严重(客服会带着 A 商品的注意事项回答
B 商品,或者把 A 的诊断写进关于 B 的触达话术)。
"""

import pytest

from app.agent.tools.product_ref import (canonical_item_ref, matches_any_item,
                                         same_item)


# --------------------------------------------------------------------------
# 归一本身
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("HMDP-1", "1"),
    ("1", "1"),
    ("  HMDP-1  ", "1"),
    ("HMDP-  1", "1"),
    # 真实 sku 原样返回:不做任何模糊匹配
    ("ACC-P1", "ACC-P1"),
    ("SHOE-270-BK-42", "SHOE-270-BK-42"),
    ("", ""),
    (None, ""),
])
def test_canonical_item_ref(raw, expected):
    assert canonical_item_ref(raw) == expected


def test_hmdp_and_bare_id_are_the_same_item():
    """这就是那两处失效的直接原因。"""
    assert same_item("HMDP-1", "1") is True
    assert same_item("1", "HMDP-1") is True


def test_different_products_are_never_conflated():
    """**过度归一比不归一更糟**:把两个不同商品判成同一个,会让客服带着 A 的注意
    事项回答 B,或把 A 的诊断写进关于 B 的话术。"""
    assert same_item("HMDP-1", "2") is False
    assert same_item("HMDP-1", "HMDP-2") is False
    assert same_item("ACC-P1", "ACC-P2") is False
    assert same_item("ACC-P1", "1") is False


def test_empty_is_never_equal():
    """"不知道"不能当"相等"用。"""
    assert same_item("", "") is False
    assert same_item(None, None) is False
    assert same_item("HMDP-1", "") is False
    assert same_item("", "1") is False


def test_matches_any_item():
    assert matches_any_item("HMDP-1", ["1"]) is True
    assert matches_any_item("HMDP-1", ["ACC-P1", "1"]) is True
    assert matches_any_item("HMDP-1", ["2", "ACC-P1"]) is False
    # 空候选集 → False:与 collab._diagnosis_applies_to 同一条纪律
    assert matches_any_item("HMDP-1", []) is False
    assert matches_any_item("HMDP-1", None) is False


# --------------------------------------------------------------------------
# 消费方一:买家侧应答提示
# --------------------------------------------------------------------------

DIAG_ENTRY = {"value": {"kind": "refund_rate_high", "subject": "HMDP-1",
                        "conclusion": "该款运动鞋实际尺码偏大,退款率 20%,超过告警线 15%"}}


def test_buyer_hint_injects_for_bare_item_id():
    """核心回归:前端传裸 id 时也要命中。修复前这里返回空串。"""
    from app.multi_agent.buyer_hints import render_buyer_hints
    out = render_buyer_hints([DIAG_ENTRY], "1")
    assert out, "前端传的裸 item_id 匹配不上 sku 形式的 subject —— 通路又断了"
    assert "退换反馈偏多" in out


def test_buyer_hint_still_injects_for_sku_form():
    from app.multi_agent.buyer_hints import render_buyer_hints
    assert render_buyer_hints([DIAG_ENTRY], "HMDP-1")


def test_buyer_hint_does_not_leak_business_numbers():
    """注入的必须是确定性文案表,**不含任何数字/指标名**。

    这是 buyer_hints 存在的全部理由:诊断的 conclusion 是写给店主的经营判断,
    直接进买家上下文,客服就可能说出"我们这款鞋退款率确实偏高"。这条断言防的是
    "顺手把 conclusion 也带上去"这种后续改动。
    """
    from app.multi_agent.buyer_hints import render_buyer_hints
    out = render_buyer_hints([DIAG_ENTRY], "1")
    for leak in ("20%", "15%", "退款率", "告警线"):
        assert leak not in out, f"经营数字/指标名泄漏进了买家侧提示: {leak}"


def test_buyer_hint_not_injected_for_a_different_product():
    """顾客在问 2 号商品,不该带着 1 号商品的注意事项去回答。"""
    from app.multi_agent.buyer_hints import render_buyer_hints
    assert render_buyer_hints([DIAG_ENTRY], "2") == ""


def test_shop_level_hint_always_injected():
    """店铺级诊断与在看哪个商品无关,恒注入。"""
    from app.multi_agent.buyer_hints import render_buyer_hints
    shop = {"value": {"kind": "angry_rate_high", "subject": "shop"}}
    assert render_buyer_hints([shop], "2")
    assert render_buyer_hints([shop], None)


# --------------------------------------------------------------------------
# 消费方二:诊断相关性闸
# --------------------------------------------------------------------------

def test_relevance_gate_matches_across_namespaces():
    """弃单商机的 sku 来自 carts.sku(裸 id),诊断 subject 来自 order_items.sku
    (HMDP- 形式)。修复前这一对永远判不适用——闸看起来在,实际全挡掉了。"""
    from app.multi_agent.collab import _diagnosis_applies_to
    diag = {"kind": "refund_rate_high", "subject": "HMDP-1"}
    assert _diagnosis_applies_to(diag, {"skus": ["1"]}) is True
    assert _diagnosis_applies_to(diag, {"skus": ["HMDP-1"]}) is True
    assert _diagnosis_applies_to(diag, {"skus": ["2"]}) is False
    assert _diagnosis_applies_to(diag, {"skus": []}) is False


# --------------------------------------------------------------------------
# 提示要具体到能挡住编造
# --------------------------------------------------------------------------

DIAG_SIZE = {"value": {
    "kind": "refund_rate_high", "subject": "HMDP-1",
    "conclusion": "该款尺码偏大…（写给店主的经营判断，含 20%/15% 这类数字）",
    "facts": {"orders": 5, "refunds": 1, "top_reason": "尺码不准，偏大一码"},
}}


def test_size_category_hint_forbids_fabrication():
    """**这是本组最重要的一条。**

    只按 kind 给的笼统提示（"退换反馈偏多…不要夸大"）挡不住编造。实测客服的回答是：

        「该款为标准版型，多数顾客反馈『尺码标准，按日常脚长选即可』；
          您平时穿42，建议继续选择 42码，无需刻意选大或选小。」

    三句全是编的——商品描述里没有任何版型信息、评价表里这款一条都没有，而真实退款
    原因写的正是"偏大一码"。**方向是反的**，顾客照这个建议下单就会收到偏大的鞋。
    """
    from app.multi_agent.buyer_hints import render_buyer_hints
    out = render_buyer_hints([DIAG_SIZE], "1")
    assert "尺码" in out, "退换原因是尺码，提示里必须点出来，否则模型会自己造一个结论"
    assert "不得断言版型标准" in out
    assert "不得编造" in out


def test_category_hint_still_leaks_nothing():
    """具体化不能以泄漏为代价:类别来自固定词表,不含数字、指标名或诊断原文。"""
    from app.multi_agent.buyer_hints import render_buyer_hints
    out = render_buyer_hints([DIAG_SIZE], "1")
    for leak in ("20%", "15%", "退款率", "告警线", "经营判断", "5", "1单"):
        assert leak not in out, f"泄漏: {leak}"


def test_reason_category_recognition():
    from app.multi_agent.buyer_hints import reason_category
    assert reason_category("尺码不准，偏大一码") == "size"
    assert reason_category("鞋子开胶了") == "quality"
    assert reason_category("颜色与描述不符") == "mismatch"
    assert reason_category("快递太慢") == "logistics"
    # 认不出就返回空(退回按 kind 的笼统提示),不硬塞一个类别——猜错类别会让客服
    # 带着错误的注意事项去回答,比只给笼统提示更糟
    assert reason_category("就是不想要了") == ""
    assert reason_category("") == ""
    assert reason_category(None) == ""


def test_unknown_category_falls_back_to_kind_hint():
    from app.multi_agent.buyer_hints import render_buyer_hints
    diag = {"value": {"kind": "refund_rate_high", "subject": "HMDP-1",
                      "facts": {"top_reason": "就是不想要了"}}}
    out = render_buyer_hints([diag], "1")
    assert "退换反馈偏多" in out, "认不出类别时应退回按 kind 的提示,而不是什么都不给"


def test_diagnosis_without_facts_still_works():
    """老诊断没有 facts 字段时不能崩(shared_context 里就存着这样的行)。"""
    from app.multi_agent.buyer_hints import render_buyer_hints
    diag = {"value": {"kind": "refund_rate_high", "subject": "HMDP-1"}}
    assert "退换反馈偏多" in render_buyer_hints([diag], "1")


def test_prompt_forbids_fabricated_aggregate_feedback():
    """prompt 侧的硬规则(纵深防御第二层)。

    这个项目自己验证过 prompt 禁令**保得住动作、保不住话术**,所以它只是第二层
    ——第一层是给模型一个**真实**的类别,让它不需要自己造一个。
    """
    from app.prompts.agents import SAFETY_RULES
    assert "多数顾客反馈" in SAFETY_RULES
    assert "版型标准" in SAFETY_RULES
    assert "不编造顾客反馈" in SAFETY_RULES
