"""G4 门禁:候选先在影子目录里跑评测,劣化即拒绝转正(fail-closed)。

注入 fake eval_fn,不触网、不跑真 LLM。
"""

from pathlib import Path

from app.agent.skills.gate import build_shadow_dir, gate_candidate

LIVE_MD = """---
name: process-return
description: 退货处理流程(现行版)。
---
现行正文。
"""

OTHER_MD = """---
name: track-order
description: 订单物流跟踪。
---
其它 skill 正文。
"""

CANDIDATE_MD = """---
name: process-return
description: 退货处理流程(候选改进版)。
---
候选正文。
"""


def _setup(tmp_path):
    definitions = tmp_path / "definitions"
    (definitions / "process-return").mkdir(parents=True)
    (definitions / "process-return" / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")
    (definitions / "track-order").mkdir(parents=True)
    (definitions / "track-order" / "SKILL.md").write_text(OTHER_MD, encoding="utf-8")
    # 候选目录嵌在 definitions 下,验证不会被复制进影子目录
    (definitions / "_candidates" / "process-return").mkdir(parents=True)
    (definitions / "_candidates" / "process-return" / "SKILL.md").write_text(
        CANDIDATE_MD, encoding="utf-8")
    return definitions


def test_build_shadow_dir_substitutes_candidate_keeps_others(tmp_path):
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"

    shadow = build_shadow_dir(str(definitions), "process-return", str(candidate),
                              str(tmp_path / "shadow"))

    assert (shadow / "process-return" / "SKILL.md").read_text(encoding="utf-8") == CANDIDATE_MD
    assert (shadow / "track-order" / "SKILL.md").read_text(encoding="utf-8") == OTHER_MD
    assert not (shadow / "_candidates").exists()   # 候选目录不进影子目录


def test_build_shadow_dir_supports_brand_new_skill(tmp_path):
    definitions = _setup(tmp_path)
    new_candidate = tmp_path / "cand" / "coupon-lookup" / "SKILL.md"
    new_candidate.parent.mkdir(parents=True)
    new_candidate.write_text(
        "---\nname: coupon-lookup\ndescription: 查券。\n---\n正文。", encoding="utf-8")

    shadow = build_shadow_dir(str(definitions), "coupon-lookup", str(new_candidate),
                              str(tmp_path / "shadow2"))

    assert (shadow / "coupon-lookup" / "SKILL.md").exists()
    assert (shadow / "process-return" / "SKILL.md").exists()


def _fake_eval(scores: dict):
    """按 skills_dir 是否为影子目录返回不同 pass_rate。"""
    calls = []

    def eval_fn(skills_dir: str, case_ids: list[str]) -> dict:
        calls.append((skills_dir, list(case_ids)))
        key = "shadow" if "shadow" in Path(skills_dir).name else "live"
        return {"summary": {"pass_rate": scores[key], "avg_process_score": None,
                            "avg_result_score": None}}

    return eval_fn, calls


def test_gate_promotes_when_candidate_better(tmp_path):
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"
    eval_fn, calls = _fake_eval({"live": 0.60, "shadow": 0.80})

    result = gate_candidate("process-return", str(candidate), str(definitions),
                            str(tmp_path / "shadow"), eval_fn, ["return_request"])

    assert result["promote"] is True
    assert result["baseline"]["pass_rate"] == 0.60
    assert result["candidate"]["pass_rate"] == 0.80
    assert len(calls) == 2
    assert calls[0][1] == ["return_request"]


def test_gate_rejects_when_candidate_regresses(tmp_path):
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"
    eval_fn, _ = _fake_eval({"live": 0.80, "shadow": 0.50})

    result = gate_candidate("process-return", str(candidate), str(definitions),
                            str(tmp_path / "shadow"), eval_fn, ["return_request"],
                            tolerance=0.05)

    assert result["promote"] is False
    assert result["comparison"]["regressed"] is True
    assert "劣化" in result["reason"]


def test_gate_tolerates_small_dip_within_tolerance(tmp_path):
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"
    eval_fn, _ = _fake_eval({"live": 0.80, "shadow": 0.78})

    result = gate_candidate("process-return", str(candidate), str(definitions),
                            str(tmp_path / "shadow"), eval_fn, ["return_request"],
                            tolerance=0.05)

    assert result["promote"] is True   # 掉 0.02 < 容差 0.05


def test_gate_fail_closed_without_cases(tmp_path):
    """没有评测用例 ⇒ 无法证明不劣化 ⇒ 拒绝转正,且不调 eval。"""
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"
    eval_fn, calls = _fake_eval({"live": 1.0, "shadow": 1.0})

    result = gate_candidate("process-return", str(candidate), str(definitions),
                            str(tmp_path / "shadow"), eval_fn, [])

    assert result["promote"] is False
    assert result["reason"] == "no_gate_cases"
    assert calls == []


def test_gate_fail_closed_when_eval_raises(tmp_path):
    definitions = _setup(tmp_path)
    candidate = definitions / "_candidates" / "process-return" / "SKILL.md"

    def boom(skills_dir, case_ids):
        raise RuntimeError("评测炸了")

    result = gate_candidate("process-return", str(candidate), str(definitions),
                            str(tmp_path / "shadow"), boom, ["return_request"])

    assert result["promote"] is False
    assert "评测炸了" in result["reason"]
