"""候选 Skill 转正 / 回滚 CLI(分级授权铁律的唯一写入点)。

本文件是全仓库**唯一**允许写 `definitions/` 正式目录的代码。写入前必须:
  ① 过静态校验(frontmatter + 工具名真实性,validator);
  ② 过灰度评测门禁(影子目录对比,gate);
  ③ 把现行版本备份到 `_archive/<name>/<时间戳>/SKILL.md`。
`--force` 只能跳过②(门禁),**不能**跳过①(校验)——编错工具名的候选永远不许上。

用法:
  python -m app.scripts.promote_skill --list                    列出候选与校验结果
  python -m app.scripts.promote_skill <skill-name>              校验+门禁+转正
  python -m app.scripts.promote_skill <skill-name> --force      跳过门禁(仍校验)
  python -m app.scripts.promote_skill <skill-name> --rollback   从最新备份恢复

备份目录 `_archive/<name>/<ts>/SKILL.md` 比正式 skill 多嵌两层,且 `_archive`
自身不含 SKILL.md,故 SkillManager._discover 不会加载它(与 `_candidates` 同理)。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.skills.validator import validate_candidate  # noqa: E402
from app.utils.console import enable_utf8_stdout  # noqa: E402

DEFINITIONS_DIR = "app/agent/skills/definitions"
CANDIDATES_DIR = "app/agent/skills/definitions/_candidates"
ARCHIVE_DIR = "app/agent/skills/definitions/_archive"


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def list_candidates(candidates_dir: str, definitions_dir: str) -> list[dict]:
    """列出候选及其校验结果。is_improvement=正式目录已存在同名 skill(改进版)。"""
    root = Path(candidates_dir)
    if not root.exists():
        return []

    items: list[dict] = []
    for skill_dir in sorted(root.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_file.exists():
            continue
        report = validate_candidate(skill_file.read_text(encoding="utf-8"))
        items.append({
            "name": skill_dir.name,
            "path": str(skill_file),
            "valid": report["valid"],
            "unknown_tools": report["unknown_tools"],
            "errors": report["errors"],
            "is_improvement": (Path(definitions_dir) / skill_dir.name / "SKILL.md").exists(),
        })
    return items


def backup_current(definitions_dir: str, skill_name: str, archive_dir: str,
                   timestamp: str) -> Path | None:
    """把现行版本备份到 archive_dir/<name>/<timestamp>/SKILL.md。

    正式目录尚无该 skill(全新候选)→ 无需备份,返回 None。
    """
    live = Path(definitions_dir) / skill_name / "SKILL.md"
    if not live.exists():
        return None

    dest_dir = Path(archive_dir) / skill_name / timestamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "SKILL.md"
    shutil.copyfile(live, dest)
    return dest


def promote(skill_name: str, definitions_dir: str, candidates_dir: str, archive_dir: str,
            gate_result: dict | None, force: bool, timestamp: str) -> dict:
    """把候选转正:校验 → 门禁 → 备份 → 写正式目录。

    返回 `{"promoted": bool, "reason": str, "backup": str | None}`。
    任一关卡不过都不写正式目录(force 只放行门禁,不放行校验)。
    """
    candidate = Path(candidates_dir) / skill_name / "SKILL.md"
    if not candidate.exists():
        return {"promoted": False, "reason": f"候选不存在: {candidate}", "backup": None}

    content = candidate.read_text(encoding="utf-8")
    report = validate_candidate(content)
    if not report["valid"]:
        return {"promoted": False,
                "reason": "校验未通过: " + "; ".join(report["errors"]), "backup": None}

    if not force:
        if gate_result is None:
            return {"promoted": False, "reason": "缺少门禁结果,拒绝转正", "backup": None}
        if not gate_result.get("promote"):
            return {"promoted": False,
                    "reason": f"门禁未通过: {gate_result.get('reason', '')}", "backup": None}

    backup = backup_current(definitions_dir, skill_name, archive_dir, timestamp)

    dest_dir = Path(definitions_dir) / skill_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "SKILL.md").write_text(content, encoding="utf-8")

    return {"promoted": True,
            "reason": "已转正" + ("(--force 跳过门禁)" if force else ""),
            "backup": str(backup) if backup else None}


def rollback(skill_name: str, definitions_dir: str, archive_dir: str) -> dict:
    """从最新备份恢复正式目录里的该 skill(劣化回滚)。"""
    skill_archive = Path(archive_dir) / skill_name
    stamps = sorted(
        (d for d in skill_archive.iterdir() if d.is_dir() and (d / "SKILL.md").exists()),
        key=lambda d: d.name,
    ) if skill_archive.exists() else []

    if not stamps:
        return {"rolled_back": False, "reason": f"无备份可回滚: {skill_archive}", "restored_from": None}

    newest = stamps[-1]
    dest_dir = Path(definitions_dir) / skill_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(newest / "SKILL.md", dest_dir / "SKILL.md")
    return {"rolled_back": True, "reason": f"已回滚到 {newest.name}",
            "restored_from": str(newest / "SKILL.md")}


def main() -> None:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="候选 Skill 转正 / 回滚")
    parser.add_argument("skill_name", nargs="?", help="要转正/回滚的 skill 名")
    parser.add_argument("--list", action="store_true", help="列出候选与校验结果")
    parser.add_argument("--force", action="store_true", help="跳过评测门禁(仍做校验)")
    parser.add_argument("--rollback", action="store_true", help="从最新备份恢复")
    args = parser.parse_args()

    if args.list:
        items = list_candidates(CANDIDATES_DIR, DEFINITIONS_DIR)
        if not items:
            print("没有候选(先跑 python -m app.scripts.synthesize_skills)")
            return
        for item in items:
            kind = "改进" if item["is_improvement"] else "新建"
            status = "✅ 可转正" if item["valid"] else f"❌ {'; '.join(item['errors'])}"
            print(f"[{kind}] {item['name']}: {status}")
        return

    if not args.skill_name:
        parser.error("需要指定 skill 名,或使用 --list")

    if args.rollback:
        print(rollback(args.skill_name, DEFINITIONS_DIR, ARCHIVE_DIR))
        return

    gate_result = None
    if not args.force:
        from app.agent.skills.gate import default_eval_fn, gate_candidate, gate_case_ids
        from app.config.settings import settings

        case_ids = gate_case_ids(args.skill_name, settings.eval_dataset_path)
        print(f"门禁用例: {case_ids or '(无 → 将拒绝转正,可用 --force 跳过门禁)'}")
        gate_result = gate_candidate(
            skill_name=args.skill_name,
            candidate_path=str(Path(CANDIDATES_DIR) / args.skill_name / "SKILL.md"),
            definitions_dir=DEFINITIONS_DIR,
            dest_root=str(Path(ARCHIVE_DIR).parent / "_shadow"),
            eval_fn=default_eval_fn,
            case_ids=case_ids,
            tolerance=settings.skill_gate_tolerance,
        )
        print(f"门禁结果: promote={gate_result['promote']} | {gate_result['reason']}")
        if gate_result.get("baseline"):
            print(f"  现行 pass_rate={gate_result['baseline'].get('pass_rate')} "
                  f"→ 候选 pass_rate={gate_result['candidate'].get('pass_rate')}")

    result = promote(args.skill_name, DEFINITIONS_DIR, CANDIDATES_DIR, ARCHIVE_DIR,
                     gate_result=gate_result, force=args.force, timestamp=_now_stamp())
    print(result)
    if result["promoted"]:
        print("已生效。如需回滚: python -m app.scripts.promote_skill "
              f"{args.skill_name} --rollback")


if __name__ == "__main__":
    main()
