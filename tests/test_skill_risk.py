"""分级授权:按引用工具与承诺措辞自动判风险档,决定放行方式。"""

from app.agent.skills.risk import (
    POLICY_CANARY_AB,
    POLICY_GATE_THEN_WATCH,
    POLICY_MANUAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    classify_risk,
    promotion_policy,
)

READONLY_MD = """---
name: order-query-all
description: 查全部订单。
---
第一步：调用 `list_user_orders`，需要明细再 `query_order`。
"""

REFUND_MD = """---
name: process-return
description: 退货处理。
---
第一步：`query_order` 核对。第二步：`apply_refund` 提交退款。
"""

BARGAIN_MD = """---
name: bargain-flow
description: 议价。
---
第一步：`query_product`。第二步：`negotiate_price` 还价。
"""

COMMITMENT_MD = """---
name: shipping-care
description: 物流关怀。
---
第一步：`query_logistics` 查轨迹。若超时，直接告知买家本单免运费。
"""


# ---------- classify_risk ----------

def test_readonly_improvement_is_low_risk():
    assert classify_risk(READONLY_MD, is_new_skill=False) == RISK_LOW


def test_readonly_new_skill_is_medium_risk():
    """全新 skill 引入新行为,线上没有对照组,至少中档。"""
    assert classify_risk(READONLY_MD, is_new_skill=True) == RISK_MEDIUM


def test_refund_tool_is_high_risk():
    assert classify_risk(REFUND_MD) == RISK_HIGH


def test_negotiate_price_is_high_risk():
    assert classify_risk(BARGAIN_MD) == RISK_HIGH


def test_commitment_wording_is_high_risk_even_without_write_tool():
    """只读工具 + 承诺免运费 → 仍是高危(承诺有资金后果)。"""
    assert classify_risk(COMMITMENT_MD) == RISK_HIGH


def test_high_risk_wins_over_new_skill_flag():
    assert classify_risk(REFUND_MD, is_new_skill=True) == RISK_HIGH


# ---------- promotion_policy ----------

def test_policy_per_risk_tier():
    assert promotion_policy(RISK_LOW) == POLICY_CANARY_AB
    assert promotion_policy(RISK_MEDIUM) == POLICY_GATE_THEN_WATCH
    assert promotion_policy(RISK_HIGH) == POLICY_MANUAL


def test_unknown_risk_falls_back_to_manual():
    """未知档位一律按最保守处理(fail-closed)。"""
    assert promotion_policy("whatever") == POLICY_MANUAL


WORKFLOW_ONLY_REFUND_MD = """---
name: refund-guarded
description: 退货处理(高危工具只出现在 workflow 声明里)。
workflow:
  guards:
    - tool: apply_refund
      requires_tools: [query_order]
      deny: '退款前先查单'
---
第一步：核对订单信息，再按既定流程处理。
"""


def test_sensitive_tool_declared_only_in_workflow_is_high_risk():
    """高危工具只写在 workflow guards(本项目给高危流程的惯用写法)也必须判高危,
    否则退款类候选会漏判成低危并进入自动上线。"""
    assert classify_risk(WORKFLOW_ONLY_REFUND_MD) == RISK_HIGH
    assert classify_risk(WORKFLOW_ONLY_REFUND_MD, is_new_skill=True) == RISK_HIGH


def test_sensitive_tool_in_requires_tools_is_high_risk():
    md = WORKFLOW_ONLY_REFUND_MD.replace(
        "tool: apply_refund", "tool: query_order").replace(
        "requires_tools: [query_order]", "requires_tools: [cancel_order]")
    assert classify_risk(md) == RISK_HIGH


def test_extended_commitment_phrases_are_high_risk():
    for phrase in ("包邮", "退运费", "退差价", "返现", "代金券", "优惠券补发"):
        md = f"""---
name: shipping-care
description: 物流关怀。
---
第一步：调用 `query_logistics` 查轨迹。若超时，告知买家本单{phrase}。
"""
        assert classify_risk(md) == RISK_HIGH, phrase


def test_plain_readonly_candidate_still_low_risk():
    """回归护栏:扩充关键词后,普通只读候选不得被误升为高危。"""
    md = """---
name: order-query-all
description: 查全部订单。
---
第一步：调用 `list_user_orders`，需要明细再 `query_order`。
"""
    assert classify_risk(md) == RISK_LOW
