"""候选 Skill 转正 / 回滚 CLI(分级授权铁律的唯一写入点)。

本文件是全仓库**唯一**写入"会被 SkillManager 加载的路径"
`definitions/<name>/SKILL.md` 的代码。注意 `definitions/` 树下另有三处写入,
但都只写 `_` 前缀的辅助目录、不会被加载:
  - `gate.build_shadow_dir` → `definitions/_shadow/`(门禁用的影子技能集);
  - `synthesizer` / `golden_corpus` → `definitions/_candidates/`(待审候选);
  - `_replace_tree` → `definitions/_swap/`(转正/回滚过程中的中转副本)。
这条隔离依赖"`_` 前缀 + 目录深度"约定(见 loader._discover),因此候选名的
路径安全校验是必需的(validator.is_safe_skill_name),否则 `../x` 之类的名字
能逃出辅助目录、直接覆盖线上 skill。

写入正式路径前必须:
  ① 过静态校验(frontmatter + 工具名真实性,validator);
  ② 过灰度评测门禁(影子目录对比,gate);
  ③ 把现行版本备份到 `_archive/<name>/<时间戳>/SKILL.md`。
`--force` 只能跳过②(门禁),**不能**跳过①(校验)——编错工具名的候选永远不许上。

`promote()` 另有一个仅供代码调用的 `block_on_high` 参数(CLI 不暴露):它是给
**无人值守**调用方(`skill_watchdog`)用的关,`high`(碰钱/承诺类)档一律拒绝
自动放行,必须靠人跑本 CLI(不传 `block_on_high`,默认 `False`)。人工路径的
既有默认行为不变。

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

from app.agent.skills import risk as risk_mod  # noqa: E402
from app.agent.skills.gate import is_safe_skill_name  # noqa: E402
from app.agent.skills.tree_text import (  # noqa: E402
    classify_tree_risk,
    has_escaping_symlink,
    validate_skill_tree,
)
from app.utils.console import enable_utf8_stdout  # noqa: E402

DEFINITIONS_DIR = "app/agent/skills/definitions"
CANDIDATES_DIR = "app/agent/skills/definitions/_candidates"
ARCHIVE_DIR = "app/agent/skills/definitions/_archive"


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _replace_tree(src: Path, dest: Path) -> None:
    """用 src 目录的内容整体替换 dest 目录(技能是**目录**,不止一个 SKILL.md)。

    中转副本放在 `<definitions>/_swap/` 下,**绝不能**放成 dest 的同级兄弟:
    SkillManager._discover 扫的正是 definitions 的直接子目录,而中转副本带着
    **同一个** frontmatter name,一旦崩溃残留就会在下次启动时静默顶掉线上版本
    (残留 .retired → 旧版复辟;残留 .staging → 未过门禁的候选直接上线),
    且全程无任何报错。

    `_swap` 之所以安全,靠的是**深度**:_discover 只把"直接子目录里直接含 SKILL.md"
    的目录当技能,而中转副本在 `_swap/<name>.staging/SKILL.md`,深了一层,扫不到
    (_discover 并没有按 `_` 前缀过滤)。另一处消费方 build_shadow_dir 则是按 `_`
    前缀排除辅助目录 —— 两处靠的性质不同,改任一处前先确认另一处仍成立。

    **本函数有两个调用方,`dest.parent` 不同,因而落出两个不同的 `_swap`**:
      - 转正/回滚:`dest.parent == definitions/`  → `definitions/_swap/`,靠上面
        那条"深度"性质对 _discover 隐身;
      - 上传端点:`dest.parent == definitions/_candidates/` → `_candidates/_swap/`,
        它对 _discover 本来就不可见(隔了两层),真正要防的是被 **list_candidates**
        当成一个候选列出来/被转正。它靠的是第三条性质:list_candidates 既要求
        `<dir>/SKILL.md` 在深度 1(而中转副本在 `_swap/<name>.staging/SKILL.md`),
        **又**用 is_safe_skill_name 拒掉 `_` 前缀的目录名。
    调用方的不变式合起来是:`dest.parent` 下的目录发现规则,必须同时拒绝
    "带 `_` 前缀的目录"与"SKILL.md 不在深度 1 的目录"。新增调用方前先确认这一点。

    换上用**两次 rename**:旧目录先改名让位,新目录立刻顶上,最后才慢慢删旧。
    这样"目标目录不存在"的窗口只有两次 rename 之间的一瞬,而不是整个 rmtree 的
    时长 —— 这点很重要,因为本函数由看门狗无人值守调用,而 SkillManager 对读不到
    的技能是**静默跳过**(线上会直接少一个技能且无任何报错)。

    为什么不逐文件覆盖:那会留下"新 SKILL.md + 旧参考资料"的半新半旧状态,
    SKILL.md 会指向已不存在的文件,比短暂窗口更糟。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    swap = dest.parent / "_swap"
    swap.mkdir(parents=True, exist_ok=True)
    staging = swap / (dest.name + ".staging")
    retired = swap / (dest.name + ".retired")
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
        if had_old:
            # 顶上失败就把旧的放回去,别让线上少一个技能。放回若也失败,
            # 也不能让它盖掉原始异常 —— 原始异常才是根因。
            try:
                retired.rename(dest)
            except OSError:
                pass
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
        # 校验的是**整棵技能树**:附带资料同样会随转正进入线上、被模型读进上下文,
        # 只审根 SKILL.md 等于放一条谁都不看的暗道。
        try:
            report = validate_skill_tree(skill_dir)
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


def _snapshot_path(definitions_dir: str, skill_name: str) -> Path:
    """算出某个候选的快照会落在哪(`_snapshot_candidate` 内部用的是同一条公式)。

    独立成一个函数是因为 `promote()` 需要在调用 `_snapshot_candidate` **之前**
    就知道这个路径 —— 这样即使快照函数本身中途炸掉(比如 copytree 抛出时快照
    目录已经落了半棵树),`promote()` 的 `finally` 依然知道该清理哪里,不必依赖
    函数正常返回值。
    """
    return Path(definitions_dir) / "_swap" / (skill_name + ".snapshot")


def _snapshot_candidate(candidate_dir: Path, definitions_dir: str, skill_name: str) -> Path:
    """把候选目录整棵树快照到 `<definitions>/_swap/<name>.snapshot/`,返回快照路径。

    放在 `_swap` 下而不是临时目录:与正式目录同卷,后续 `_replace_tree` 的 copytree
    才不会退化成跨卷复制;而 `_swap` 对 _discover / list_candidates 的隐身性质见
    `_replace_tree` 的 docstring(`.snapshot` 与 `.staging`/`.retired` 三者不重名)。

    **符号链接逃逸 = 拒绝快照(fail-closed)**:`read_skill_tree`/`classify_tree_risk`
    刻意不把解析后逃出候选目录的文件纳入审核面(模型经 loader 也读不到它)——但
    `shutil.copytree` 默认会**解引用**符号链接,把逃逸目标的内容原样当成普通文件
    复制进快照,而不是复制符号链接本身。这样判档时"看不到"的字节,装机时却会
    被原样搬进正式目录:候选目录外任意一份磁盘文件(比如系统配置、其他技能的
    未公开草稿)都能通过一个符号链接被"洗"成审核通过的技能内容。两个可选修法
    (二选一,这里选前者):
      a) 发现逃逸符号链接就直接拒绝快照(本实现);
      b) 用 `copytree(..., symlinks=True)` 保留符号链接本身,不解引用。
    选 a 而不是 b:b 会把一个真实指向候选目录之外的符号链接对象原样装进正式
    目录——虽然 `loader.read_skill_file`/`list_skill_files` 现在会因为解析后越界
    而拒绝读它,但这依赖"以后没有代码路径会绕过那条解析检查"这个假设一直成立;
    a 则是从根上不让这种候选转正,逼一个人去把候选目录本身修干净(去掉逃逸链接
    或把内容改成候选目录内部的普通文件),不依赖任何下游代码永远做对。
    """
    if has_escaping_symlink(candidate_dir):
        raise OSError("候选目录内存在解析后逃出目录的符号链接(审核面看不到,但 "
                      "copytree 会把目标内容原样复制进去),拒绝快照转正")

    snapshot = _snapshot_path(definitions_dir, skill_name)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.exists():
        shutil.rmtree(snapshot)
    shutil.copytree(candidate_dir, snapshot)
    return snapshot


def promote(skill_name: str, definitions_dir: str, candidates_dir: str, archive_dir: str,
            gate_result: dict | None, force: bool, timestamp: str,
            *, block_on_high: bool = False) -> dict:
    """把候选转正:快照 → 校验 → 门禁 → (可选)风险门 → 备份 → 写正式目录。

    返回 `{"promoted", "reason", "backup", "risk"}`。
    任一关卡不过都不写正式目录(force 只放行门禁,不放行校验)。

    **为什么先快照**:原来是"读候选 SKILL.md 文本 → 校验 → backup_current 整树
    copytree(耗时) → _replace_tree 再从磁盘重读候选目录"。校验过的字节与最终装上
    线的字节之间隔着一整段可写窗口 —— 而 `POST /api/admin/skills/upload` 现在能从
    网页并发替换那个候选目录。先把候选整棵树快照下来,**校验、判档、安装全部只认
    这一份快照**,TOCTOU 就在构造上消失了。快照路径由 `_snapshot_path` 提前算出
    (不依赖 `_snapshot_candidate` 正常返回),因此下面唯一的 `finally` 在所有退出
    路径上都能清到它——包括 `_snapshot_candidate` 自己中途失败、已经落了半棵树
    的情形(修复前 `except OSError` 分支直接 return,不走任何 `finally`,那半棵
    树会一直留在 `_swap/` 下)。

    `risk` 按快照的**整棵树**判档(含附带资料)。默认 `block_on_high=False`:
    高危档的含义是"必须由人来放行",而人跑 `promote_skill <name>` 正是那个放行
    动作,所以人工路径默认不拦、只如实报出 `risk`。`block_on_high=True` 是给
    **无人值守**的调用方(`skill_watchdog`)用的关:它对**这份快照**判档,而
    快照就是即将被装上线的那份字节本身——判档看到的与装机装的是同一份内容,
    不会再有"看的是一份、装的是另一份"的 TOCTOU 窗口。
    """
    if not is_safe_skill_name(skill_name):
        return {"promoted": False, "reason": f"非法 skill 名,拒绝操作: {skill_name!r}",
                "backup": None, "risk": None}

    candidate = Path(candidates_dir) / skill_name / "SKILL.md"
    if not candidate.exists():
        return {"promoted": False, "reason": f"候选不存在: {candidate}",
                "backup": None, "risk": None}

    snapshot = _snapshot_path(definitions_dir, skill_name)
    try:
        try:
            _snapshot_candidate(candidate.parent, definitions_dir, skill_name)
        except OSError as exc:
            return {"promoted": False, "reason": f"候选快照失败,拒绝转正: {exc}",
                    "backup": None, "risk": None}

        report = validate_skill_tree(snapshot)
        if not report["valid"]:
            return {"promoted": False,
                    "reason": "校验未通过: " + "; ".join(report["errors"]),
                    "backup": None, "risk": None}

        if report["name"] != skill_name:
            return {"promoted": False,
                    "reason": f"frontmatter name {report['name']!r} 与目标 skill 名 {skill_name!r} 不一致,"
                              "拒绝转正(会让线上目录注册成另一个名字,真 skill 从目录中消失)",
                    "backup": None, "risk": None}

        if not force:
            if gate_result is None:
                return {"promoted": False, "reason": "缺少门禁结果,拒绝转正",
                        "backup": None, "risk": None}
            if not gate_result.get("promote"):
                return {"promoted": False,
                        "reason": f"门禁未通过: {gate_result.get('reason', '')}",
                        "backup": None, "risk": None}

        is_new = not (Path(definitions_dir) / skill_name / "SKILL.md").exists()
        risk = classify_tree_risk(snapshot, is_new_skill=is_new, tree=report["tree"])

        if block_on_high and risk_mod.promotion_policy(risk) == risk_mod.POLICY_MANUAL:
            return {"promoted": False,
                    "reason": f"整棵技能树判档为高风险({risk}),自动化转正拒绝放行,"
                              f"需人工执行: python -m app.scripts.promote_skill {skill_name}",
                    "backup": None, "risk": risk}

        backup = backup_current(definitions_dir, skill_name, archive_dir, timestamp)

        # 整目录替换,且**装的就是刚校验过的那份快照**(候选可能带 references 等
        # 附带资料;直接从 _candidates 重读会重新打开那扇 TOCTOU 窗口)
        _replace_tree(snapshot, Path(definitions_dir) / skill_name)

        return {"promoted": True,
                "reason": "已转正" + ("(--force 跳过门禁)" if force else ""),
                "backup": str(backup) if backup else None, "risk": risk}
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)


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
                    from app.agent.skills.risk import promotion_policy

                    # 判档看**整棵技能树**:附带资料里的动钱指令同样会随转正上线
                    _risk = classify_tree_risk(Path(item["path"]).parent,
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
