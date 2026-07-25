"""政策问答强制检索:三域 prompt 硬约束 + evaluator 接地覆盖政策断言。纯文本断言。"""

from app.prompts.agents import PRESALE_PROMPT, MIDSALE_PROMPT, AFTERSALE_PROMPT
from app.prompts.reply_pipeline import EVALUATOR_PROMPT


def test_all_domains_forbid_unsourced_policy():
    for p in (PRESALE_PROMPT, MIDSALE_PROMPT, AFTERSALE_PROMPT):
        assert "search_knowledge" in p
        assert "严禁凭记忆" in p or "禁止凭记忆" in p    # 负面硬约束存在
        assert "政策" in p


def test_evaluator_grounding_covers_policy_claims():
    assert "政策" in EVALUATOR_PROMPT
    assert "订单号格式" in EVALUATOR_PROMPT or "格式" in EVALUATOR_PROMPT
    # 明确"无检索支撑的政策断言=不接地"
    assert "没有检索" in EVALUATOR_PROMPT or "无检索" in EVALUATOR_PROMPT or "检索结果" in EVALUATOR_PROMPT
