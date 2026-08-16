"""从真实会话为 skill 合成门禁用例(解开"新 skill 永远转不了正"的死结)。

用法:
  python -m app.scripts.synth_gate_cases --skill order-query   为一个 skill 合成
  python -m app.scripts.synth_gate_cases --all                 正式目录 + 候选目录全跑
  python -m app.scripts.synth_gate_cases --all --dry-run       只看会合成出什么,不落盘

产物落 `app/evaluation/cases_synth/<skill>.json`,**不进** `cases.json`——
后者同时是回归基线的采样集,混入未经人工审核的用例会让基线漂移。

生成逻辑一次 LLM 都不调,全部断言取自事实,见
`app/agent/skills/case_synthesis.py` 的模块 docstring。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.skills.case_synthesis import (  # noqa: E402
    DEFAULT_MAX_CASES,
    synthesize_and_save,
    synthesize_gate_cases,
)
from app.utils.console import enable_utf8_stdout  # noqa: E402

_AUX_PREFIX = "_"


def list_all_skills(skills_dir: str) -> list[str]:
    """正式目录 + 候选目录里的全部 skill 名(候选在后,同名去重)。

    候选**必须**包含:新蒸馏的候选正是最需要合成用例的那一类——它还没进正式目录,
    也还没有任何执行轨迹,而门禁在它转正之前就要跑。
    """
    names: list[str] = []
    for base in (Path(skills_dir), Path(skills_dir) / "_candidates"):
        if not base.exists():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith(_AUX_PREFIX):
                continue
            if (child / "SKILL.md").exists() and child.name not in names:
                names.append(child.name)
    return names


def _report(stats: dict) -> None:
    skill = stats.get("skill")
    kept = stats.get("kept", 0)
    if not kept:
        # **0 条必须说清是为什么。** 一行光秃秃的"0 条"读起来像"线上没这类会话",
        # 而实测四种原因完全不同:卖家侧 skill 根本没有对应语料、frontmatter 没
        # 声明关键词、素材有但断不出期望、SKILL.md 找不到。它们要人做的事不一样。
        why = []
        if not stats.get("skill_md_found", True):
            why.append("找不到 SKILL.md")
        if stats.get("keyword_blocked"):
            # 卖家侧被挡掉关键词路。这条理由要说全:不是"没素材",是**按关键词
            # 捞会捞到买家提问,据此生成的用例是错的而不只是弱的**。
            why.append("卖家侧 skill,关键词路已禁用(会捞到买家会话,生成的用例是错的);"
                       "当前无该 skill 的执行轨迹")
        elif stats.get("actor") == "seller":
            why.append("卖家侧 skill:归档语料目前只有买家会话")
        if not stats.get("keywords"):
            why.append("frontmatter 未声明关键词(无法走关键词采样)")
        if stats.get("dropped_no_assertion"):
            why.append(f"{stats['dropped_no_assertion']} 轮断不出任何期望")
        if not why:
            why.append(f"扫了 {stats.get('sessions_scanned', 0)} 个归档会话,没有命中的轮次")
        print(f"  {skill:<32} 0 条  ({'; '.join(why)})")
        return
    print(f"  {skill:<32} {kept} 条  "
          f"(轨迹 {stats.get('from_trace', 0)} / 关键词 {stats.get('from_keyword', 0)})"
          + (f"  → {stats['saved']}" if stats.get("saved") else "  [dry-run 未落盘]"))


def main(argv=None) -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--skill", help="只为这一个 skill 合成")
    group.add_argument("--all", action="store_true", help="正式目录 + 候选目录全跑")
    parser.add_argument("--max-cases", type=int, default=DEFAULT_MAX_CASES,
                        help=f"每个 skill 最多合成几条(默认 {DEFAULT_MAX_CASES})")
    parser.add_argument("--dry-run", action="store_true", help="只打印,不落盘")
    args = parser.parse_args(argv)

    from app.config.settings import settings
    from app.db import get_db

    db = get_db()
    names = [args.skill] if args.skill else list_all_skills(settings.skills_dir)
    if not names:
        print("没有找到任何 skill。")
        return 1

    print(f"为 {len(names)} 个 skill 合成门禁用例"
          f"{'(dry-run)' if args.dry_run else ''}:")
    total = 0
    for name in names:
        if args.dry_run:
            skill_md = ""
            for base in (Path(settings.skills_dir),
                         Path(settings.skills_dir) / "_candidates"):
                p = base / name / "SKILL.md"
                if p.exists():
                    skill_md = p.read_text(encoding="utf-8")
                    break
            cases, stats = synthesize_gate_cases(
                name, traces=db.list_skill_traces(skill_name=name, limit=500),
                archives=db.list_recent_archives(limit=300), skill_md=skill_md,
                max_cases=args.max_cases)
            stats["skill_md_found"] = bool(skill_md)
        else:
            stats = synthesize_and_save(name, db=db, max_cases=args.max_cases)
        total += stats.get("kept", 0)
        _report(stats)

    print(f"\n共 {total} 条。这些用例**未经人工审核**,只用于转正门禁,"
          f"不进回归基线({settings.eval_dataset_path})。")
    print("门禁读取时会把人工用例与合成用例分别报数(gate_readiness 的 "
          "human_count / synthetic_count),界面上如实标注。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
