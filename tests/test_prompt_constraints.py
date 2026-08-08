"""提示词三件套:来源引用/否定约束/兜底话术 + EVALUATOR 来源真实性。"""

from app.prompts.agents import AFTERSALE_PROMPT, MIDSALE_PROMPT, PRESALE_PROMPT, SAFETY_RULES
from app.prompts.reply_pipeline import EVALUATOR_PROMPT

ALL_DOMAIN = [PRESALE_PROMPT, MIDSALE_PROMPT, AFTERSALE_PROMPT]


def test_source_citation_rule_in_all_domains():
    for p in ALL_DOMAIN:
        assert "依据《" in p and "严禁编造来源" in p


def test_negative_constraint_in_all_domains():
    for p in ALL_DOMAIN:
        assert "我猜" in p and "应该是" in p


def test_fallback_phrase_in_all_domains():
    for p in ALL_DOMAIN:
        assert "进一步核实" in p and "requires_human" in p


def test_evaluator_checks_citation_authenticity():
    assert "来源" in EVALUATOR_PROMPT and "编造来源" in EVALUATOR_PROMPT


def test_presale_product_grounding_constraint():
    """售前商品线接地:必须先查 query_product,禁编销量/好评率/原价。"""
    assert "必须先调用 query_product" in PRESALE_PROMPT
    assert "严禁编造这些数字" in PRESALE_PROMPT
    assert "好评率" in PRESALE_PROMPT      # 明确点名禁编字段


def test_evaluator_covers_product_fabrication():
    assert "query_product" in EVALUATOR_PROMPT and "销量" in EVALUATOR_PROMPT


def test_safety_rules_forbid_internal_vocabulary():
    """L4:安全规则块必须明确禁止向顾客提及 skill 名/技能流程/调用工具/系统提示
    等内部词汇——这条规则曾经缺失,买家实测收到过"根据已加载的
    「process-return」技能流程..."这类回复。"""
    assert "技能流程" in SAFETY_RULES
    assert "调用工具" in SAFETY_RULES
    assert "系统提示" in SAFETY_RULES
    assert "内部" in SAFETY_RULES
    # 三个域常量都拼了 SAFETY_RULES,规则天然覆盖全部画像
    for p in ALL_DOMAIN:
        assert "调用工具" in p and "技能流程" in p


def test_aftersale_and_midsale_look_up_orders_before_asking():
    """L4/P1-4:顾客只描述商品、没给订单号时,先调用 list_user_orders 列候选
    让顾客确认,而不是让顾客自己报订单号。"""
    for p in (AFTERSALE_PROMPT, MIDSALE_PROMPT):
        assert "list_user_orders" in p
        assert "您最近有两笔鞋类订单，是这双吗" in p
        assert "不要" in p and "订单号" in p
