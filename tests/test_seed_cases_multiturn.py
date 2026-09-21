"""WS5 多轮种子用例:入库即校验(人工集,进回归基线)。

论文最有说服力的失败模式——"第一轮说反规则但答案自洽、用户不放弃"——单轮 QA
几乎不可能触发;这 5 条种子把多轮失败模式钉进人工集。本测试守住:它们真的在
cases.json 里、真的多轮、能被 EvalCase 加载、id 不与合成源(synth-)混淆。
"""

from pathlib import Path

from app.evaluation.dataset import load_dataset

CASES = Path(__file__).resolve().parent.parent / "app" / "evaluation" / "cases.json"
SEED_IDS = {
    "seed_mt_policy_hold_under_pushback",
    "seed_mt_anaphora_logistics",
    "seed_mt_repeat_escalation",
    "seed_mt_domain_switch_refund",
    "seed_mt_anaphora_coupon",
}


def test_seed_cases_present_and_multiturn():
    cases = {c.id: c for c in load_dataset(CASES)}
    assert SEED_IDS <= set(cases)
    for cid in SEED_IDS:
        assert len(cases[cid].turns) >= 2, cid


def test_policy_hold_case_asserts_real_policy():
    """施压轮的存在 + 真实政策关键词断言,缺一不可——否则测不出"持守"。"""
    cases = {c.id: c for c in load_dataset(CASES)}
    c = cases["seed_mt_policy_hold_under_pushback"]
    assert any("免费" in t for t in c.turns)      # 施压话术
    assert "由您承担" in c.expected_keywords      # 真实政策方向


def test_repeat_case_asserts_escalation():
    cases = {c.id: c for c in load_dataset(CASES)}
    c = cases["seed_mt_repeat_escalation"]
    assert len(c.turns) == 3
    assert c.expected_requires_human is True


def test_seed_ids_do_not_collide_with_synth_prefix():
    """synth- 前缀是"未经人工审核"的全局可见标记;种子是人工集,不许混形。"""
    cases = list(load_dataset(CASES))
    for c in cases:
        if c.id in SEED_IDS:
            assert not c.id.startswith("synth-")
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))
