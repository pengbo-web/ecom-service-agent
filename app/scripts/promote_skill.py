"""候选 Skill 转正 / 回滚 CLI(分级授权铁律的唯一写入点)。

本文件是全仓库**唯一**写入"会被 SkillManager 加载的路径"
`definitions/<name>/SKILL.md` 的代码。注意 `definitions/` 树下另有两处写入,
但都只写 `_` 前缀的辅助目录、不会被加载:
  - `gate.build_shadow_dir` → `definitions/_shadow/`(门禁用的影子技能集);
  - `synthesizer` / `golden_corpus` → `definitions/_candidates/`(待审候选)。
这条隔离依赖"`_` 前缀 + 目录深度"约定(见 loader._discover),因此候选名的
路径安全校验是必需的(validator.is_safe_skill_name),否则 `../x` 之类的名字
能逃出辅助目录、直接覆盖线上 skill。

写入正式路径前必须:
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

from app.agent.skills.gate import is_safe_skill_name  # noqa: E402
from app.agent.skills.validator import validate_candidate  # noqa: E402
from app.utils.console import enable_utf8_stdout  # noqa: E402

DEFINITIONS_DIR = "app/agent/skills/definitions"
CANDIDATES_DIR = "app/agent/skills/definitions/_candidates"
ARCHIVE_DIR = "app/agent/skills/definitions/_archive"


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _replace_tree(src: Path, dest: Path) -> None:
    """用 src 目录的内容整体替换 dest 目录(技能是**目录**,不止一个 SKILL.md)。

    先把新内容复制到同级 .staging,再**两次 rename**换上:旧目录先改名让位,
    新目录立刻顶上,最后才慢慢删旧。这样"目标目录不存在"的窗口只有两次 rename
    之间的一瞬,而不是整个 rmtree 的时长 —— 这点很重要,因为本函数会由看门狗
    无人值守调用,而 SkillManager 对读不到的技能是**静默跳过**(线上会直接少一个
    技能且无任何报错)。

    为什么不逐文件覆盖:那会留下"新 SKILL.md + 旧参考资料"的半新半旧状态,
    SKILL.md 会指向已不存在的文件,比短暂窗口更糟。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(dest.name + ".staging")
    retired = dest.with_name(dest.name + ".retired")
    for leftover in (staging, retired):
        if leftover.exists():
            shutil.rmtree(leftover)

    shutil.copytree(src, staging)
    had_old = dest.exists()
    if had_old:
        dest.rename(retired)          # 让位(瞬时)
    try:
        staging.rename(dest)          # 顶上(瞬时)
    except OSError:
        if had_old:                   # 顶上失败就把旧的放回去,别让线上少一个技能
            retired.rename(dest)
        raise
    if had_old:
        shutil.rmtree(retired, ignore_errors=True)


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
        if not is_safe_skill_name(skill_dir.name):
            continue   # 目录名不合法(不可能是我们写出的候选),跳过
        # 逐项容错:单个候选文件读不出/解不开不该让整份清单崩掉,标为不合法继续列。
        try:
            report = validate_candidate(skill_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            report = {"valid": False, "unknown_tools": [],
                      "errors": [f"读取候选失败: {type(exc).__name__}: {exc}"]}

        # 目录名必须与 frontmatter 的 name 一致:否则影子目录会把候选放进 <dir> 槽位,
        # 而 SkillManager 按 frontmatter name 注册 → 被测 skill 直接从影子集里消失,
        # 基线与候选打成平手 → 门禁误判"无劣化"而放行。
        declared = str(report.get("name") or "")
        if declared and declared != skill_dir.name:
            report = {"valid": False, "unknown_tools": report.get("unknown_tools", []),
                      "errors": list(report.get("errors", []))
                      + [f"目录名 {skill_dir.name!r} 与 frontmatter name {declared!r} 不一致"]}

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
    """把现行技能**整个目录**备份到 archive_dir/<name>/<timestamp>/。

    技能是目录(可带 references 等参考资料),只备份 SKILL.md 会让回滚丢附件。
    正式目录尚无该技能(全新候选)→ 无需备份,返回 None。
    """
    if not is_safe_skill_name(skill_name):
        return None
    live_dir = Path(definitions_dir) / skill_name
    if not (live_dir / "SKILL.md").exists():
        return None

    dest_dir = Path(archive_dir) / skill_name / timestamp
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(live_dir, dest_dir)
    return dest_dir / "SKILL.md"


def promote(skill_name: str, definitions_dir: str, candidates_dir: str, archive_dir: str,
            gate_result: dict | None, force: bool, timestamp: str) -> dict:
    """把候选转正:校验 → 门禁 → 备份 → 写正式目录。

    返回 `{"promoted": bool, "reason": str, "backup": str | None}`。
    任一关卡不过都不写正式目录(force 只放行门禁,不放行校验)。
    """
    if not is_safe_skill_name(skill_name):
        return {"promoted": False, "reason": f"非法 skill 名,拒绝操作: {skill_name!r}",
                "backup": None}

    candidate = Path(candidates_dir) / skill_name / "SKILL.md"
    if not candidate.exists():
        return {"promoted": False, "reason": f"候选不存在: {candidate}", "backup": None}

    content = candidate.read_text(encoding="utf-8")
    report = validate_candidate(content)
    if not report["valid"]:
        return {"promoted": False,
                "reason": "校验未通过: " + "; ".join(report["errors"]), "backup": None}

    if report["name"] != skill_name:
        return {"promoted": False,
                "reason": f"frontmatter name {report['name']!r} 与目标 skill 名 {skill_name!r} 不一致,"
                          "拒绝转正(会让线上目录注册成另一个名字,真 skill 从目录中消失)",
                "backup": None}

    if not force:
        if gate_result is None:
            return {"promoted": False, "reason": "缺少门禁结果,拒绝转正", "backup": None}
        if not gate_result.get("promote"):
            return {"promoted": False,
                    "reason": f"门禁未通过: {gate_result.get('reason', '')}", "backup": None}

    backup = backup_current(definitions_dir, skill_name, archive_dir, timestamp)

    # 整目录替换:候选可能带 references 等附带资料,只写 SKILL.md 会让它指向不存在的文件
    _replace_tree(candidate.parent, Path(definitions_dir) / skill_name)

    return {"promoted": True,
            "reason": "已转正" + ("(--force 跳过门禁)" if force else ""),
            "backup": str(backup) if backup else None}


def rollback(skill_name: str, definitions_dir: str, archive_dir: str) -> dict:
    """从最新备份恢复正式目录里的该 skill(劣化回滚)。"""
    if not is_safe_skill_name(skill_name):
        return {"rolled_back": False, "reason": f"非法 skill 名,拒绝操作: {skill_name!r}",
                "restored_from": None}

    skill_archive = Path(archive_dir) / skill_name
    stamps = sorted(
        (d for d in skill_archive.iterdir()
         if d.is_dir() and not d.name.startswith("rejected-") and (d / "SKILL.md").exists()),
        key=lambda d: d.name,
    ) if skill_archive.exists() else []

    if not stamps:
        return {"rolled_back": False, "reason": f"无备份可回滚: {skill_archive}", "restored_from": None}

    newest = stamps[-1]
    # 整目录还原:备份里含当时的全部附带资料,只还原 SKILL.md 会留下上一版的残余附件
    _replace_tree(newest, Path(definitions_dir) / skill_name)
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
            tier = ""
            if item["valid"]:
                try:
                    from app.agent.skills.risk import classify_risk, promotion_policy
                    _risk = classify_risk(Path(item["path"]).read_text(encoding="utf-8"),
                                          is_new_skill=not item["is_improvement"])
                    tier = f" | 风险={_risk} 放行={promotion_policy(_risk)}"
                except Exception:  # noqa: BLE001 判档失败不影响列表可用
                    tier = " | 风险=未知(判档失败)"
            print(f"[{kind}] {item['name']}: {status}{tier}")
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
