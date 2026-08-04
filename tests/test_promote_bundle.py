"""多文件技能包在转正/备份/回滚/影子目录里必须整体搬运,不能只搬 SKILL.md。"""

from pathlib import Path

from app.agent.skills.gate import build_shadow_dir
from app.scripts.promote_skill import backup_current, promote, rollback

LIVE_MD = """---
name: demo-skill
description: 现行版。适用关键词：演示。
---
现行正文。详见 references/live.md。
"""

CAND_MD = """---
name: demo-skill
description: 候选版。适用关键词：演示。
---
候选正文。详见 references/cand.md。
"""

PASS_GATE = {"promote": True, "reason": "候选未劣化,允许转正"}


def _setup(tmp_path):
    defs = tmp_path / "definitions"
    live = defs / "demo-skill"
    (live / "references").mkdir(parents=True)
    (live / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")
    (live / "references" / "live.md").write_text("现行参考", encoding="utf-8")

    cand = defs / "_candidates" / "demo-skill"
    (cand / "references").mkdir(parents=True)
    (cand / "SKILL.md").write_text(CAND_MD, encoding="utf-8")
    (cand / "references" / "cand.md").write_text("候选参考", encoding="utf-8")

    return str(defs), str(defs / "_candidates"), str(defs / "_archive")


def test_backup_copies_whole_directory(tmp_path):
    defs, _, archive = _setup(tmp_path)
    out = backup_current(defs, "demo-skill", archive, "20260804-120000")

    assert out is not None
    stamp_dir = Path(archive) / "demo-skill" / "20260804-120000"
    assert (stamp_dir / "SKILL.md").read_text(encoding="utf-8") == LIVE_MD
    assert (stamp_dir / "references" / "live.md").read_text(encoding="utf-8") == "现行参考"


def test_promote_brings_bundled_files_and_drops_stale_ones(tmp_path):
    defs, cand, archive = _setup(tmp_path)
    result = promote("demo-skill", defs, cand, archive, PASS_GATE, False, "t1")

    assert result["promoted"] is True
    live = Path(defs) / "demo-skill"
    assert (live / "SKILL.md").read_text(encoding="utf-8") == CAND_MD
    assert (live / "references" / "cand.md").read_text(encoding="utf-8") == "候选参考"
    # 现行版原有的 references/live.md 不属于候选包,转正后不应残留
    assert not (live / "references" / "live.md").exists()


def test_rollback_restores_whole_directory(tmp_path):
    defs, cand, archive = _setup(tmp_path)
    promote("demo-skill", defs, cand, archive, PASS_GATE, False, "20260804-120000")

    result = rollback("demo-skill", defs, archive)

    assert result["rolled_back"] is True
    live = Path(defs) / "demo-skill"
    assert (live / "SKILL.md").read_text(encoding="utf-8") == LIVE_MD
    assert (live / "references" / "live.md").read_text(encoding="utf-8") == "现行参考"
    assert not (live / "references" / "cand.md").exists()


def test_shadow_dir_copies_bundled_files(tmp_path):
    defs, cand, _ = _setup(tmp_path)
    shadow = build_shadow_dir(defs, "demo-skill",
                              str(Path(cand) / "demo-skill" / "SKILL.md"),
                              str(tmp_path / "shadow"))

    assert (shadow / "demo-skill" / "SKILL.md").read_text(encoding="utf-8") == CAND_MD
    # 影子集里必须有候选自带的参考资料,否则等于在评一个残缺技能
    assert (shadow / "demo-skill" / "references" / "cand.md").exists()


def test_shadow_dir_skips_aux_dirs(tmp_path):
    """_candidates / _archive 这类辅助目录不属于技能集,不进影子目录。"""
    defs, cand, archive = _setup(tmp_path)
    Path(archive).mkdir(parents=True, exist_ok=True)
    shadow = build_shadow_dir(defs, "demo-skill",
                              str(Path(cand) / "demo-skill" / "SKILL.md"),
                              str(tmp_path / "shadow2"))
    assert not (shadow / "_candidates").exists()
    assert not (shadow / "_archive").exists()


def test_archive_rejected_moves_whole_candidate_dir(tmp_path):
    from app.scripts.skill_watchdog import _archive_rejected

    defs, cand, archive = _setup(tmp_path)
    out = _archive_rejected("demo-skill", cand, archive)

    assert out is not None
    assert not (Path(cand) / "demo-skill").exists()      # 候选整个被移走
    moved = list(Path(archive).glob("demo-skill/rejected-*/references/cand.md"))
    assert moved, "附带资料也应一并归档"


def test_swap_leftovers_are_not_discovered_as_live_skills(tmp_path):
    """中转副本带着同一个 frontmatter name:若放成 definitions 的同级兄弟,
    崩溃残留会在下次启动时静默顶掉线上版本。必须落在不被扫描的 _swap/ 下。"""
    from app.agent.skills.loader import SkillManager

    defs, cand, archive = _setup(tmp_path)
    promote("demo-skill", defs, cand, archive, PASS_GATE, False, "t1")

    # 模拟崩溃残留:两种中转副本都放上
    swap = Path(defs) / "_swap"
    swap.mkdir(parents=True, exist_ok=True)
    for suffix in (".staging", ".retired"):
        leftover = swap / ("demo-skill" + suffix)
        leftover.mkdir(parents=True, exist_ok=True)
        (leftover / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")

    mgr = SkillManager(skills_dir=defs, enabled=True)

    assert mgr.skill_names == ["demo-skill"]          # 残留没被当成技能加载
    live = (Path(defs) / "demo-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert live == CAND_MD                            # 线上仍是刚转正的候选版,没被复辟


def test_replace_tree_leaves_no_leftovers_on_success(tmp_path):
    """正常路径跑完不留中转副本(留了就是下次启动的隐患)。"""
    defs, cand, archive = _setup(tmp_path)
    promote("demo-skill", defs, cand, archive, PASS_GATE, False, "t1")

    swap = Path(defs) / "_swap"
    assert not swap.exists() or list(swap.iterdir()) == []


def test_shadow_dir_excludes_live_only_files(tmp_path):
    """影子集里被测技能应是候选的完整内容:现行版独有的参考资料不该残留进来。"""
    defs, cand, _ = _setup(tmp_path)
    shadow = build_shadow_dir(defs, "demo-skill",
                              str(Path(cand) / "demo-skill" / "SKILL.md"),
                              str(tmp_path / "shadow3"))

    assert (shadow / "demo-skill" / "references" / "cand.md").exists()
    assert not (shadow / "demo-skill" / "references" / "live.md").exists()
