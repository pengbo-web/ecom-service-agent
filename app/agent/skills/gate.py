"""G4 候选灰度评测门禁:候选先在"影子技能目录"里跑一遍评测,和现行版本比,
劣化超过容差就拒绝转正。

这是自进化敢落地的前提——多客的说法是"新旧 Skill 灰度对比成功率,劣化版本回滚"。
本模块只负责判定(promote true/false),真正的写入/备份/回滚在
`app/scripts/promote_skill.py`。

fail-closed 原则:没有相关评测用例、或评测本身抛异常,都返回 promote=False。
"不能证明更好"就不许上,绝不默认放行。

影子目录:把 definitions/ 下的正式 skill 全量复制一份(跳过 _ 前缀的辅助目录),
再用候选覆盖/新增目标 skill。这样评测跑的是"只换了这一个 skill"的完整技能集。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.evaluation.regression import compare_to_baseline

# definitions/ 下这些前缀的目录是辅助目录(候选/备份),不属于正式技能集
_AUX_PREFIX = "_"


def build_shadow_dir(definitions_dir: str, skill_name: str,
                     candidate_path: str, dest_root: str) -> Path:
    """构建影子技能目录:正式技能全量复制 + 用候选覆盖(或新增)目标 skill。

    dest_root 若已存在会被清空重建,保证每次门禁跑在干净目录上。
    """
    src = Path(definitions_dir)
    dest = Path(dest_root)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    for child in sorted(src.iterdir()):
        if not child.is_dir() or child.name.startswith(_AUX_PREFIX):
            continue
        skill_file = child / "SKILL.md"
        if not skill_file.exists():
            continue
        target = dest / child.name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_file, target / "SKILL.md")

    # 候选覆盖/新增目标 skill(新建 skill 时正式目录里还没有它)
    target = dest / skill_name
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(candidate_path), target / "SKILL.md")
    return dest


def gate_candidate(skill_name: str, candidate_path: str, definitions_dir: str,
                   dest_root: str, eval_fn, case_ids: list[str],
                   tolerance: float = 0.05) -> dict:
    """灰度对比候选与现行版本,判定是否允许转正。

    - `eval_fn(skills_dir: str, case_ids: list[str]) -> report`,report 含 "summary";
      注入式设计,单测传 fake、生产传 default_eval_fn。
    - 无 case_ids → promote=False, reason="no_gate_cases"(fail-closed);
    - eval_fn 抛异常 → promote=False,reason 带异常信息(fail-closed);
    - 复用 `compare_to_baseline`:候选相对现行掉点超过 tolerance 即判劣化。
    """
    if not case_ids:
        return {"promote": False, "reason": "no_gate_cases", "baseline": None,
                "candidate": None, "comparison": None, "shadow_dir": None}

    try:
        shadow = build_shadow_dir(definitions_dir, skill_name, candidate_path, dest_root)
        baseline = (eval_fn(definitions_dir, case_ids) or {}).get("summary") or {}
        candidate = (eval_fn(str(shadow), case_ids) or {}).get("summary") or {}
    except Exception as exc:  # noqa: BLE001 门禁 fail-closed:评测失败=不许上
        return {"promote": False, "reason": f"评测执行失败: {exc}", "baseline": None,
                "candidate": None, "comparison": None, "shadow_dir": None}

    comparison = compare_to_baseline(candidate, baseline, tolerance)
    regressed = comparison["regressed"]
    reason = "候选劣化超过容差,拒绝转正" if regressed else "候选未劣化,允许转正"
    return {"promote": not regressed, "reason": reason, "baseline": baseline,
            "candidate": candidate, "comparison": comparison, "shadow_dir": str(shadow)}


def gate_case_ids(skill_name: str, dataset_path: str) -> list[str]:
    """取与该 skill 相关的评测用例 id(供 CLI 组装门禁入参)。"""
    from app.evaluation.dataset import filter_by_skill, load_dataset

    return [c.id for c in filter_by_skill(load_dataset(dataset_path), skill_name)]


def default_eval_fn(skills_dir: str, case_ids: list[str]) -> dict:
    """真实评测实现:临时把 settings.skills_dir 指到给定目录,只跑指定用例。

    会真调 LLM(评测本身要跑 Agent),故单测不用本函数。settings 改动在
    finally 中还原,避免污染同进程后续调用。
    """
    from openai import OpenAI

    from app.config.settings import settings
    from app.evaluation.dataset import load_dataset
    from app.evaluation.evaluator import Evaluator
    from app.evaluation.sandbox import Sandbox

    original_dir = settings.skills_dir
    settings.skills_dir = skills_dir
    try:
        cases = [c for c in load_dataset(settings.eval_dataset_path) if c.id in set(case_ids)]
        client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        evaluator = Evaluator(
            sandbox=Sandbox(mode="single"), client=client, model=settings.model_name,
            use_judge=settings.eval_use_judge, pass_threshold=settings.eval_pass_threshold,
        )
        return evaluator.run_all(cases)
    finally:
        settings.skills_dir = original_dir
