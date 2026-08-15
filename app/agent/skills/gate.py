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

from app.agent.skills.validator import is_safe_skill_name  # noqa: F401  (转出:供 promote CLI 复用同一判定)
from app.evaluation.regression import compare_to_baseline

# definitions/ 下这些前缀的目录是辅助目录(候选/备份),不属于正式技能集
_AUX_PREFIX = "_"


def build_shadow_dir(definitions_dir: str, skill_name: str,
                     candidate_path: str, dest_root: str) -> Path:
    """构建影子技能目录:正式技能全量复制 + 用候选覆盖(或新增)目标 skill。

    dest_root 若已存在会被清空重建,保证每次门禁跑在干净目录上。

    skill_name 非法(不是单个安全路径段)时抛 ValueError。
    """
    if not is_safe_skill_name(skill_name):
        raise ValueError(f"非法 skill 名(必须是单个安全路径段): {skill_name!r}")

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
        # 整目录复制:技能可带参考资料,影子集缺了附件等于在评一个残缺技能
        shutil.copytree(child, dest / child.name)

    # 候选覆盖/新增目标 skill(新建 skill 时正式目录里还没有它)
    target = dest / skill_name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(Path(candidate_path).parent, target)
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

    返回值除判定外还带三个**证据元数据**,供调用方如实转述(见 `gate_readiness`):

    - `evaluable`:门禁到底评没评。`False` 覆盖"没有用例"/"评测跑崩"/"没产出
      可比指标"三种——它们全都是 `promote=False`,但都**不代表候选不达标**。
      调用方拿它区分"评不了"与"评了没过":混成一条,操作者会去修一个没问题的候选。
    - `underpowered`:用例数少于 `MIN_TRUSTWORTHY_CASES`,结论是噪声级的。
    - `case_count`:实际跑了几条。

    阶段一 gap⑤:本函数是 `promote_skill.py`/`skill_watchdog.py` 这两条离线 CLI
    调门禁的入口,整段判定过程(含两次真调 LLM 的 `eval_fn` 调用)包进一条
    命名 trace,门控关/未装/异常时 `background_trace` 静默让本函数行为不变。
    """
    if not case_ids:
        # `evaluable=False` 是给调用方的:这一条**不是**"候选没通过评测",而是
        # "根本没评"。看门狗/界面必须据此打出不同的标签与文案,否则操作者会去
        # 修一个完全没有问题的候选(实测新蒸馏的 skill 全落在这一支)。
        return {"promote": False, "reason": "no_gate_cases", "baseline": None,
                "candidate": None, "comparison": None, "shadow_dir": None,
                "evaluable": False, "underpowered": False, "case_count": 0}

    underpowered = len(case_ids) < MIN_TRUSTWORTHY_CASES

    from app.observability.langfuse_bridge import background_trace

    with background_trace("skill_gate_candidate",
                          input={"skill_name": skill_name, "case_ids": case_ids}) as root:
        try:
            shadow = build_shadow_dir(definitions_dir, skill_name, candidate_path, dest_root)
            baseline = (eval_fn(definitions_dir, case_ids) or {}).get("summary") or {}
            candidate = (eval_fn(str(shadow), case_ids) or {}).get("summary") or {}
        except Exception as exc:  # noqa: BLE001 门禁 fail-closed:评测失败=不许上
            # 评测**跑崩了**同样属于"评不了"(evaluable=False):候选本身没被否证。
            return {"promote": False, "reason": f"评测执行失败: {exc}", "baseline": None,
                    "candidate": None, "comparison": None, "shadow_dir": None,
                    "evaluable": False, "underpowered": underpowered,
                    "case_count": len(case_ids)}

        comparison = compare_to_baseline(candidate, baseline, tolerance)
        # fail-closed:没有任何可比指标(评测返回空/缺 summary/指标全为 None)时,
        # compare_to_baseline 会给出 regressed=False —— 那是"没测出劣化",不是"证明了不劣化",
        # 绝不能据此放行。
        if not comparison["diffs"]:
            # 同上:"没测出劣化"是评不动,不是候选不达标。
            return {"promote": False, "reason": "评测未产出可比指标,按 fail-closed 拒绝转正",
                    "baseline": baseline or None, "candidate": candidate or None,
                    "comparison": comparison, "shadow_dir": str(shadow),
                    "evaluable": False, "underpowered": underpowered,
                    "case_count": len(case_ids)}
        regressed = comparison["regressed"]
        reason = "候选劣化超过容差,拒绝转正" if regressed else "候选未劣化,允许转正"
        # 样本太少时把这句话**贴在结论上**。放行判定不变(仍按是否劣化),但
        # `promote=True` 后面必须紧跟着证据强度,不能让 n=1 的对比在日志和界面上
        # 长得跟一次真正的回归评测一模一样。
        if underpowered:
            reason += f"(注意:仅 {len(case_ids)} 条用例,证据强度不足以支撑自动上线)"
        if root is not None:
            try:
                root.update(output={"promote": not regressed, "reason": reason})
            except Exception:  # noqa: BLE001 记录失败不影响门禁判定
                pass
        return {"promote": not regressed, "reason": reason, "baseline": baseline,
                "candidate": candidate, "comparison": comparison, "shadow_dir": str(shadow),
                "evaluable": True, "underpowered": underpowered,
                "case_count": len(case_ids)}


def gate_case_ids(skill_name: str, dataset_path: str,
                  include_synthetic: bool = True) -> list[str]:
    """取与该 skill 相关的评测用例 id(供 CLI 组装门禁入参)。

    两个来源,人工的在前:

    - **人工集**(`dataset_path`,即 `cases.json`):现有逻辑一字未改。
    - **合成集**(`cases_synth/<skill>.json`):从真实会话自动合成、未经人工审核,
      见 `app.agent.skills.case_synthesis`。它的存在是为了解开"新 skill 没有用例
      → 门禁评不了 → 永远转不了正"这个死结。

    合成集读不出来时**静默退回只用人工集**:它是增量能力,坏掉时应该回到原状态,
    而不是连人工用例也跑不了。`settings.skill_gate_synth_enabled=False` 可一键退回。

    要分别报数请用 `gate_readiness()` —— 它给 `human_count` / `synthetic_count`。
    只报一个总数会让操作者以为这个 skill 有人工把关过。
    """
    from app.evaluation.dataset import filter_by_skill, load_dataset

    human = [c.id for c in filter_by_skill(load_dataset(dataset_path), skill_name)]
    if not include_synthetic:
        return human

    try:
        from app.agent.skills.case_synthesis import load_synth_cases
        from app.config.settings import settings

        if not settings.skill_gate_synth_enabled:
            return human
        synth = [c.id for c in load_synth_cases(skill_name) if c.id not in set(human)]
    except Exception:  # noqa: BLE001 合成集是增量能力,坏了就当没有
        synth = []
    return human + synth


# 门禁比的是 pass_rate / avg_process_score / avg_result_score 三个**均值**
# (见 app/evaluation/regression.py `_METRICS`),两侧各真调一次 LLM 跑同一批用例。
# n 很小时这个差值的主体是 LLM 的运行间噪声,不是候选与现行的真实差异:
# tolerance=0.05 对 n=1 意味着"只要候选没把唯一那条用例从过弄成不过就算未劣化",
# 而反过来一次随机波动也能凭空判出劣化。三条是能让"均值"这个词勉强站得住的下限。
MIN_TRUSTWORTHY_CASES = 3


def gate_readiness(skill_name: str, dataset_path: str) -> dict:
    """门禁**跑得动吗、跑出来的结论可信吗** —— 在花钱跑两轮评测之前就能回答。

    分三态,而不是"能/不能"两态。这个区分是本函数存在的全部理由:

    - `evaluable=False`(一条用例都没有):门禁**评不了**。这不是"候选不达标",
      调用方必须把它和真的评测未通过分开报——`gate_candidate` 对这种情况返回
      `promote=False, reason="no_gate_cases"`,而下游一路折成 `gate_failed`,
      操作者看到的是"门禁未通过",于是去改候选,而候选没有任何问题。
    - `underpowered=True`(有,但少于 `MIN_TRUSTWORTHY_CASES`):门禁跑得动,
      结论是噪声级的。它会输出 `promote=True` —— 读起来像"过了评测",
      实际证据强度接近零,不该据此**自动**上线。
    - 都为假:正常。

    `dataset_path` 读不出来(文件缺失/格式坏)按 `evaluable=False` 处理并在 note 里
    说明原因:读不出数据集时同样是"评不了",绝不能因为异常就当作"没有相关用例"
    这种听起来很正常的结论。
    """
    try:
        human_ids = gate_case_ids(skill_name, dataset_path, include_synthetic=False)
        case_ids = gate_case_ids(skill_name, dataset_path)
        error = ""
    except Exception as exc:  # noqa: BLE001 读不出数据集 = 评不了,不是"没有用例"
        human_ids, case_ids, error = [], [], f"评测数据集读取失败: {exc}"

    count = len(case_ids)
    human_count = len(human_ids)
    synthetic_count = count - human_count

    # **合成用例必须单独报数,不能只给一个总数。** 一个"5 条门禁用例"的 skill,
    # 如果那 5 条全是机器从真实会话里自动合成、没有人看过一眼的,它与一个有 5 条
    # 人工用例的 skill 在证据强度上完全不是一回事。只报总数会让操作者以为这个
    # skill 有人工把过关——那正是自动化最容易骗到人的地方。
    synth_suffix = ""
    if synthetic_count:
        synth_suffix = (f",其中 {synthetic_count} 条为自动合成、**未经人工审核**"
                        "(从真实会话生成,断言取自真实工具调用与人工坐席回复)")

    if error:
        note = error + " —— 门禁无法评估"
    elif count == 0:
        note = (f"评测集里没有任何用例点名覆盖 {skill_name}(用例的 related_skills 字段),"
                "且没有可用的自动合成用例,门禁无法评估;这不是候选质量问题")
    elif count < MIN_TRUSTWORTHY_CASES:
        note = (f"仅 {count} 条门禁用例(建议至少 {MIN_TRUSTWORTHY_CASES} 条):"
                "两轮评测各真调一次 LLM,这么少的样本上差值以运行噪声为主,"
                "门禁结论不足以作为自动上线的依据" + synth_suffix)
    else:
        note = f"{count} 条门禁用例" + synth_suffix

    return {"case_ids": case_ids, "count": count,
            "human_count": human_count, "synthetic_count": synthetic_count,
            "evaluable": count > 0, "underpowered": 0 < count < MIN_TRUSTWORTHY_CASES,
            "note": note}


def resolve_cases(case_ids: list[str]) -> list:
    """按 id 把用例取出来,**人工集与合成集一起找**,顺序与 `case_ids` 一致。

    单独成函数是为了能在不真调 LLM 的前提下测到它。这一步少了合成集那一半,
    **整个阶段一等于没做**:`gate_case_ids` 会把合成用例的 id 交进来,而这里若
    只读 `cases.json` 就一条都匹配不上 → 评测跑 0 条 → summary 为空 → 门禁按
    fail-closed 打"评测未产出可比指标"——从看门狗日志上看,和从前的
    `gate_unavailable` 一模一样,没有人能发现合成用例根本没被跑过。
    """
    from app.agent.skills.case_synthesis import load_all_synth_cases
    from app.config.settings import settings
    from app.evaluation.dataset import load_dataset

    by_id = {c.id: c for c in load_dataset(settings.eval_dataset_path)}
    for case in load_all_synth_cases():
        by_id.setdefault(case.id, case)     # 人工用例同 id 时以人工的为准
    return [by_id[cid] for cid in case_ids if cid in by_id]


def default_eval_fn(skills_dir: str, case_ids: list[str]) -> dict:
    """真实评测实现:临时把 settings.skills_dir 指到给定目录,只跑指定用例。

    会真调 LLM(评测本身要跑 Agent),故单测不用本函数。settings 改动在
    finally 中还原,避免污染同进程后续调用。
    """
    from app.config.settings import settings
    from app.evaluation.evaluator import Evaluator
    from app.evaluation.sandbox import Sandbox
    from app.observability.langfuse_client import make_openai_client

    original_dir = settings.skills_dir
    settings.skills_dir = skills_dir
    try:
        cases = resolve_cases(case_ids)
        client = make_openai_client(api_key=settings.openai_api_key,
                                    base_url=settings.openai_base_url)
        evaluator = Evaluator(
            sandbox=Sandbox(mode="single"), client=client, model=settings.model_name,
            use_judge=settings.eval_use_judge, pass_threshold=settings.eval_pass_threshold,
        )
        return evaluator.run_all(cases)
    finally:
        settings.skills_dir = original_dir
