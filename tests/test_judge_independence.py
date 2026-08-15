"""Generator ≠ Evaluator:写东西的模型不能给自己打分。

论文把这一条列为**唯一的架构性硬要求**。改造前本仓库三处全是 `settings.model_name`:
沙箱里跑的客服 Agent(产出被评答案)、LLM-as-judge(给答案打分)、skill 编辑器
(写 SKILL.md)。而门禁比的三个指标里,`avg_result_score` 与 `avg_process_score`
**主要由 judge 打出来** —— 也就是说"候选到底更好了没有"这个判定,由被判定方
自己的同款模型给出。

本文件守两件事,第二件比第一件重要:
1. 裁判模型可以独立配置,且换的是**裁判**不是被评对象(换错了等于在评一个
   不存在的线上行为);
2. **同模型时必须显形。** 默认值仍是"沿用主模型"(硬改默认会让没配第二个端点的
   部署第一次跑门禁就 404),所以唯一的防线是把"此刻在自审"如实说出来。
   一个不知道自己在自审的 81 分,比一个标着"自审"的 81 分危险得多。
"""

import pytest

from app.evaluation.independence import (
    agent_model,
    editor_model,
    independence_report,
    judge_model,
)


@pytest.fixture
def cfg(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "model_name", "main-model")
    monkeypatch.setattr(settings, "eval_judge_model", "")
    monkeypatch.setattr(settings, "eval_judge_base_url", "")
    monkeypatch.setattr(settings, "eval_judge_api_key", "")
    monkeypatch.setattr(settings, "skill_editor_model", "")
    return settings


# --------------------------------------------------------------------------
# 默认行为:留空 = 沿用主模型,逐字节不变
# --------------------------------------------------------------------------

def test_empty_config_keeps_the_previous_behaviour(cfg):
    """默认值不能改成某个具体模型名——没配那个端点的部署会在第一次跑门禁时 404。
    那是把一条架构建议变成一次线上故障。"""
    assert judge_model() == "main-model"
    assert editor_model() == "main-model"
    assert agent_model() == "main-model"


def test_blank_string_is_treated_as_unset(cfg, monkeypatch):
    """`.env` 里写 `EVAL_JUDGE_MODEL=` 或几个空格,与没写是一回事。"""
    monkeypatch.setattr(cfg, "eval_judge_model", "   ")
    assert judge_model() == "main-model"


# --------------------------------------------------------------------------
# 自审必须显形
# --------------------------------------------------------------------------

def test_same_model_is_reported_as_self_review(cfg):
    r = independence_report()
    assert r["independent"] is False
    assert "裁判与被评 Agent 同模型" in r["self_review"]
    assert "裁判与 skill 编辑器同模型" in r["self_review"]


def test_self_review_note_names_the_affected_metrics(cfg):
    """光说一句"同模型"没用。要说清**哪几个数字**因此不能当独立证据——
    门禁比的正是 avg_result_score / avg_process_score。"""
    note = independence_report()["note"]
    assert "自审" in note
    for metric in ("answer_quality", "faithfulness", "process_soundness"):
        assert metric in note
    assert "avg_result_score" in note and "avg_process_score" in note
    assert "EVAL_JUDGE_MODEL" in note      # 还要说清怎么解除


def test_independent_config_is_reported_as_such(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "eval_judge_model", "judge-model")
    monkeypatch.setattr(cfg, "skill_editor_model", "editor-model")
    r = independence_report()
    assert r["independent"] is True
    assert r["self_review"] == []
    assert "自审" not in r["note"]


def test_judge_differing_from_agent_but_equal_to_editor_still_flags(cfg, monkeypatch):
    """**这一条是最容易漏的组合。** 裁判换掉了、看起来独立了,但它与写 SKILL.md
    的编辑器仍是同一个 —— 门禁在评它自己改出来的东西,循环依赖原封不动。"""
    monkeypatch.setattr(cfg, "eval_judge_model", "shared")
    monkeypatch.setattr(cfg, "skill_editor_model", "shared")
    r = independence_report()
    assert r["independent"] is False
    assert r["self_review"] == ["裁判与 skill 编辑器同模型"]


# --------------------------------------------------------------------------
# 换的是裁判,不是被评对象
# --------------------------------------------------------------------------

def test_changing_the_judge_never_changes_the_agent(cfg, monkeypatch):
    """**换错了就等于在评一个不存在的线上行为。**

    沙箱里跑的 Agent 必须仍是线上那个模型,否则评测结果与生产没有任何关系。
    """
    monkeypatch.setattr(cfg, "eval_judge_model", "judge-model")
    assert agent_model() == "main-model"
    assert cfg.model_name == "main-model"


def test_judge_client_falls_back_to_the_main_endpoint(cfg, monkeypatch):
    """只换模型名、不换端点是最常见的用法(同一个端点上有多个模型可选)。"""
    monkeypatch.setattr(cfg, "openai_base_url", "https://main.example/v1")
    monkeypatch.setattr(cfg, "openai_api_key", "main-key")
    monkeypatch.setattr(cfg, "eval_judge_model", "judge-model")
    client = judge_client_or_skip()
    assert str(client.base_url).rstrip("/") == "https://main.example/v1"


def test_judge_client_uses_its_own_endpoint_when_configured(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "openai_base_url", "https://main.example/v1")
    monkeypatch.setattr(cfg, "openai_api_key", "main-key")
    monkeypatch.setattr(cfg, "eval_judge_base_url", "https://judge.example/v1")
    monkeypatch.setattr(cfg, "eval_judge_api_key", "judge-key")
    client = judge_client_or_skip()
    assert str(client.base_url).rstrip("/") == "https://judge.example/v1"


def judge_client_or_skip():
    from app.evaluation.independence import judge_client

    return judge_client()


# --------------------------------------------------------------------------
# 结论必须跟着分数走
# --------------------------------------------------------------------------

def test_summary_carries_the_independence_verdict_when_judge_is_on(cfg):
    """摘要会被门禁、看门狗、管理端各自转述。把这个事实留在原地,
    是让它一路跟到人眼前的唯一办法。"""
    from app.evaluation.evaluator import Evaluator

    ev = Evaluator(sandbox=None, client=None, model="judge-model", use_judge=True)
    summary = ev._aggregate([])["summary"]
    assert summary["judge_independence"]["independent"] is False


def test_summary_omits_the_verdict_when_judge_is_off(cfg):
    """关掉 judge 时全是代码规则打分,不存在自审问题。凭空多一行免责声明
    只会变成噪声,而噪声读几次之后就没人再读了。"""
    from app.evaluation.evaluator import Evaluator

    ev = Evaluator(sandbox=None, client=None, model="m", use_judge=False)
    assert "judge_independence" not in ev._aggregate([])["summary"]


def test_gate_reason_carries_the_self_review_warning(cfg, tmp_path):
    """无人值守路径上 `reason` 是唯一留痕。「候选未劣化,允许转正」这句话
    若不带上"这是自评",日志翻回来时没人知道这次转正凭的是什么证据。"""
    from app.agent.skills.gate import gate_candidate

    (tmp_path / "defs" / "s").mkdir(parents=True)
    (tmp_path / "defs" / "s" / "SKILL.md").write_text("x", encoding="utf-8")
    cand = tmp_path / "cand" / "s"
    cand.mkdir(parents=True)
    (cand / "SKILL.md").write_text("y", encoding="utf-8")

    def fake_eval(skills_dir, case_ids):
        return {"summary": {"pass_rate": 0.9, "avg_process_score": 0.9,
                            "avg_result_score": 0.9,
                            "judge_independence": independence_report()}}

    r = gate_candidate("s", str(cand / "SKILL.md"), str(tmp_path / "defs"),
                       str(tmp_path / "shadow"), fake_eval, ["c1", "c2", "c3"])
    assert r["promote"] is True
    assert "自审" in r["reason"]


def test_gate_reason_stays_clean_when_the_judge_is_independent(cfg, monkeypatch, tmp_path):
    from app.agent.skills.gate import gate_candidate

    monkeypatch.setattr(cfg, "eval_judge_model", "judge-model")
    monkeypatch.setattr(cfg, "skill_editor_model", "editor-model")
    (tmp_path / "defs" / "s").mkdir(parents=True)
    (tmp_path / "defs" / "s" / "SKILL.md").write_text("x", encoding="utf-8")
    cand = tmp_path / "cand" / "s"
    cand.mkdir(parents=True)
    (cand / "SKILL.md").write_text("y", encoding="utf-8")

    def fake_eval(skills_dir, case_ids):
        return {"summary": {"pass_rate": 0.9, "avg_process_score": 0.9,
                            "avg_result_score": 0.9,
                            "judge_independence": independence_report()}}

    r = gate_candidate("s", str(cand / "SKILL.md"), str(tmp_path / "defs"),
                       str(tmp_path / "shadow"), fake_eval, ["c1", "c2", "c3"])
    assert "自审" not in r["reason"]


def test_this_deployment_is_actually_independent():
    """**本部署的实际配置**(读 .env)。不是断言"能配",是断言"配了"。

    实测同一个 DashScope 端点上 qwen-max / qwen-turbo / deepseek-v3 / qwen3-max
    都可调,所以这里不需要第二个端点就能做到裁判独立 —— 没有理由不做。
    这条测试红了说明有人把 EVAL_JUDGE_MODEL 删了或改回了主模型。
    """
    r = independence_report()
    assert r["independent"] is True, r["note"]
