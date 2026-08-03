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
