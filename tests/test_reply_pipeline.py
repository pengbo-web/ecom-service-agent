"""H1.1:回复流水线三提示词（评估器 / 重写 / 润色）的字符串常量测试。

本任务只测试提示词常量本身（非空 + 含关键约束词），
不测试任何流水线逻辑（流水线由 H1.2 的 ReplyPipeline 实现）。
"""

from app.prompts.reply_pipeline import (
    EVALUATOR_PROMPT,
    REDRAFT_PROMPT,
    POLISH_PROMPT,
)


def test_evaluator_prompt_nonempty_and_grounded():
    assert EVALUATOR_PROMPT.strip()
    assert "接地" in EVALUATOR_PROMPT


def test_redraft_prompt_nonempty_and_fact_based():
    assert REDRAFT_PROMPT.strip()
    assert "事实" in REDRAFT_PROMPT


def test_polish_prompt_nonempty_and_fact_locked():
    assert POLISH_PROMPT.strip()
    assert "不得改变任何事实" in POLISH_PROMPT
