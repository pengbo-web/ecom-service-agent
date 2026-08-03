"""G4 前置:评测用例按 skill 打标,门禁只跑与候选相关的子集(控成本)。"""

import json

from app.evaluation.dataset import EvalCase, filter_by_skill, load_dataset


def test_eval_case_defaults_related_skills_empty():
    case = EvalCase(id="x", description="d", turns=["hi"])
    assert case.related_skills == []


def test_filter_by_skill_selects_tagged_cases():
    cases = [
        EvalCase(id="a", description="", turns=["1"], related_skills=["process-return"]),
        EvalCase(id="b", description="", turns=["2"], related_skills=["track-order"]),
        EvalCase(id="c", description="", turns=["3"]),
    ]
    assert [c.id for c in filter_by_skill(cases, "process-return")] == ["a"]
    assert filter_by_skill(cases, "unknown-skill") == []


def test_load_dataset_reads_related_skills(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [
        {"id": "a", "description": "d", "turns": ["hi"], "related_skills": ["process-return"]},
        {"id": "b", "description": "d", "turns": ["hi"]},
    ]}, ensure_ascii=False), encoding="utf-8")

    cases = load_dataset(path)
    assert cases[0].related_skills == ["process-return"]
    assert cases[1].related_skills == []


def test_real_dataset_has_skill_tagged_cases():
    """正式数据集必须有打标用例,否则门禁永远 fail-closed 拒绝一切候选。"""
    cases = load_dataset("app/evaluation/cases.json")
    tagged = [c for c in cases if c.related_skills]
    assert tagged, "cases.json 中至少要有一条 related_skills 打标用例"
    assert filter_by_skill(cases, "process-return")
