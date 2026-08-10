"""回归:营销 Agent 的 prompt 必须与权威商机表同步,不许再出现手抄快照。

背景(本项目"手抄一份会漂移"的第 6 次):GROWTH_PROMPT 曾经写死三个 kind,
还带一句明确的错误断言——「本店订单表没有"未支付"状态,所以没有催付款这类
商机」。N5 之后 `unpaid_order`/`abandoned_cart`/`shipped_no_care`/
`delivered_no_review` 都已经是真实可查的商机(工具 schema 那边早改成派生
渲染),唯独这份 prompt 快照没同步——营销 Agent 的第一信源于是在告诉它
"催付款这件事不存在",而那恰恰是它最该干的活。

所以这里钉的不是"prompt 里有没有某个词",是**派生关系本身**:任何一个
kind 只要在 OPPORTUNITY_KINDS 里,就必须出现在 prompt 里。新增第八个 kind
而忘了同步 prompt,这条测试会红。
"""

from __future__ import annotations

from app.agent.tools.growth import OPPORTUNITY_KINDS
from app.prompts.seller_agents import GROWTH_PROMPT


def test_prompt_lists_every_opportunity_kind():
    missing = [k for k in OPPORTUNITY_KINDS if k not in GROWTH_PROMPT]
    assert not missing, (
        f"GROWTH_PROMPT 未包含这些商机类型: {missing}。"
        "prompt 里的清单必须从 OPPORTUNITY_KINDS 渲染,不要手抄。"
    )


def test_prompt_lists_every_kind_label():
    """连中文说明也要来自权威表——只列 key 不列含义,模型仍要靠猜。"""
    missing = [v for v in OPPORTUNITY_KINDS.values() if v not in GROWTH_PROMPT]
    assert not missing, f"GROWTH_PROMPT 未包含这些商机类型的中文说明: {missing}"


def test_prompt_does_not_deny_unpaid_opportunities():
    """具体钉住那句错误断言,防原样复活。

    "加购未付款 / 大量未支付订单" 是营销增长最核心的场景;prompt 里一旦再出现
    "没有未支付""没有催付款"这类否定,Agent 就又会对店主说这件事做不了。
    """
    for bad in ("没有\"未支付\"状态", "没有催付款", "没有未支付"):
        assert bad not in GROWTH_PROMPT, f"GROWTH_PROMPT 又出现了错误断言: {bad}"
