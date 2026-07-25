"""提示词三件套:来源引用/否定约束/兜底话术 + EVALUATOR 来源真实性。"""

from app.prompts.agents import AFTERSALE_PROMPT, MIDSALE_PROMPT, PRESALE_PROMPT
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
