"""G6 候选校验:frontmatter 完整性 + 引用的工具名必须真实存在于 registry。

实测背景:LLM 合成的候选写过 `order_list`(真实是 list_user_orders)、
`coupon_query`(真实是 query_coupons),直接转正会让 Agent 调到不存在的工具。
"""

from app.agent.skills.validator import known_tool_names, referenced_tools, validate_candidate

GOOD = """---
name: order-query-all
description: 用户请求查询名下全部订单时触发。
---
第一步：调用 `list_user_orders` 查询用户名下全部订单。
第二步：需要单笔明细时调用 `query_order`。
"""

BAD_TOOL = """---
name: order-query-all
description: 用户请求查询名下全部订单时触发。
---
第一步：调用 `order_list` 查询用户名下全部订单。
第二步：优惠券用 `coupon_query` 查询。
"""

NO_FRONTMATTER = "这是一段没有 frontmatter 的纯文本。"

MISSING_DESC = """---
name: order-query-all
---
正文。
"""


def test_known_tool_names_contains_real_tools():
    names = known_tool_names()
    assert "list_user_orders" in names
    assert "query_coupons" in names
    assert "load_skill" in names
    assert "order_list" not in names


def test_referenced_tools_extracts_backticked_snake_case():
    refs = referenced_tools(GOOD)
    assert refs == {"list_user_orders", "query_order"}


def test_referenced_tools_ignores_prose_and_non_identifiers():
    content = "第一步：先确认订单，再走 `七天无理由` 流程，参考 `Nike Air` 商品。"
    assert referenced_tools(content) == set()


def test_validate_good_candidate_passes():
    result = validate_candidate(GOOD)
    assert result["valid"] is True
    assert result["name"] == "order-query-all"
    assert result["description"]
    assert result["unknown_tools"] == []
    assert result["errors"] == []


def test_validate_flags_unknown_tools():
    result = validate_candidate(BAD_TOOL)
    assert result["valid"] is False
    assert result["unknown_tools"] == ["coupon_query", "order_list"]   # 已排序
    assert any("未知工具" in e for e in result["errors"])


def test_validate_rejects_missing_frontmatter():
    result = validate_candidate(NO_FRONTMATTER)
    assert result["valid"] is False
    assert result["name"] == ""
    assert any("frontmatter" in e for e in result["errors"])


def test_validate_rejects_missing_description():
    result = validate_candidate(MISSING_DESC)
    assert result["valid"] is False
    assert any("description" in e for e in result["errors"])


def test_validate_accepts_injected_known_set():
    """允许注入工具清单,便于单测与未来多套工具集。"""
    result = validate_candidate(BAD_TOOL, known={"order_list", "coupon_query"})
    assert result["valid"] is True
    assert result["unknown_tools"] == []


# ---------- G1:workflow 声明里的工具名同样要校验 ----------

WORKFLOW_GOOD = """---
name: process-return
description: 退货处理。
workflow:
  guards:
    - tool: apply_refund
      requires_tools: [query_order]
      deny: '先查单'
---
第一步：调用 `query_order`。
"""

WORKFLOW_BAD_TOOL = """---
name: process-return
description: 退货处理。
workflow:
  guards:
    - tool: refund_apply
      requires_tools: [order_lookup]
      deny: '先查单'
---
正文不引用任何工具。
"""


def test_validate_accepts_workflow_with_real_tools():
    result = validate_candidate(WORKFLOW_GOOD)
    assert result["valid"] is True
    assert result["unknown_tools"] == []


def test_validate_flags_unknown_tools_in_workflow_block():
    """声明里的错工具比正文里的更危险:会让守卫拦死真实调用。"""
    result = validate_candidate(WORKFLOW_BAD_TOOL)
    assert result["valid"] is False
    assert result["unknown_tools"] == ["order_lookup", "refund_apply"]


def test_validate_merges_body_and_workflow_unknown_tools():
    content = WORKFLOW_BAD_TOOL.replace("正文不引用任何工具。", "还要调用 `bogus_tool`。")
    result = validate_candidate(content)
    assert set(result["unknown_tools"]) == {"bogus_tool", "order_lookup", "refund_apply"}


def test_validate_ignores_malformed_workflow_block():
    content = """---
name: x
description: d
workflow: 这不是字典
---
调用 `query_order`。
"""
    result = validate_candidate(content)
    assert result["valid"] is True     # 坏声明不额外报错(加载时同样按无约束处理)
