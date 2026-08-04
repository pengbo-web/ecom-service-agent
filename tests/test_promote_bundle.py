"""多文件技能包在转正/备份/回滚/影子目录里必须整体搬运,不能只搬 SKILL.md。"""

from pathlib import Path

import pytest

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


def test_interrupted_swap_leaves_nothing_discoverable(tmp_path, monkeypatch):
    """中断的换目录不得在被扫描的层级留下中转副本。

    模拟"顶上"那一步失败:修复前中转副本是 definitions 的**同级兄弟**,带着同一个
    frontmatter name,残留会被 _discover 加载、静默顶掉线上版本;修复后它们在
    _swap/ 下(深度上就不会被发现)。本测试因此能证伪修复前的实现。
    """
    from app.agent.skills.loader import SkillManager
    from app.scripts import promote_skill as ps

    defs, cand, archive = _setup(tmp_path)
    real_rename = Path.rename

    def boom(self, target):
        # 只让"staging → 正式目录"这一步失败,回滚那一步照常成功
        if self.name.endswith(".staging"):
            raise OSError("模拟崩溃:顶上失败")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", boom)
    with pytest.raises(OSError):
        ps._replace_tree(Path(cand) / "demo-skill", Path(defs) / "demo-skill")
    monkeypatch.undo()

    # definitions 的直接子目录里不得出现任何中转副本(修复前这里会有 demo-skill.staging)
    strays = [p.name for p in Path(defs).iterdir()
              if p.is_dir() and (".staging" in p.name or ".retired" in p.name)]
    assert strays == [], strays

    # 回滚分支把旧版放了回去,线上技能仍可被正确发现,且是现行版而非候选版
    mgr = SkillManager(skills_dir=defs, enabled=True)
    assert mgr.skill_names == ["demo-skill"]
    assert (Path(defs) / "demo-skill" / "SKILL.md").read_text(encoding="utf-8") == LIVE_MD


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


# ---------- 转正装的必须是"过了关卡的那份字节"(TOCTOU) ----------

def test_promote_installs_the_snapshot_it_validated(tmp_path, monkeypatch):
    """校验之后、装机之前候选目录被并发改写(上传端点现在能从网页做到这件事),
    线上装上去的仍必须是**过关的那一份**,而不是改写后的新内容。

    做法:在 backup_current 执行的那一刻(校验已过、_replace_tree 尚未执行)偷偷
    把候选目录换掉,再断言线上落地的是原始候选。
    """
    from app.scripts import promote_skill as ps

    defs, cand, archive = _setup(tmp_path)
    real_backup = ps.backup_current

    def _tamper(*args, **kwargs):
        out = real_backup(*args, **kwargs)
        (Path(cand) / "demo-skill" / "SKILL.md").write_text(
            CAND_MD.replace("候选正文", "被偷换的正文"), encoding="utf-8")
        (Path(cand) / "demo-skill" / "references" / "evil.md").write_text(
            "偷塞的附件", encoding="utf-8")
        return out

    monkeypatch.setattr(ps, "backup_current", _tamper)

    result = ps.promote("demo-skill", defs, cand, archive, PASS_GATE, False, "t1")

    assert result["promoted"] is True
    live = Path(defs) / "demo-skill"
    assert (live / "SKILL.md").read_text(encoding="utf-8") == CAND_MD
    assert not (live / "references" / "evil.md").exists()


def test_promote_cleans_up_snapshot_on_every_path(tmp_path):
    """快照放在 _swap 下,成功与失败路径都必须清干净(残留会被下次误用/占盘)。"""
    from app.scripts.promote_skill import promote

    defs, cand, archive = _setup(tmp_path)
    promote("demo-skill", defs, cand, archive, PASS_GATE, False, "t1")
    swap = Path(defs) / "_swap"
    assert not swap.exists() or list(swap.iterdir()) == []

    # 失败路径(门禁未过)同样不许留快照
    promote("demo-skill", defs, cand, archive,
            {"promote": False, "reason": "劣化"}, False, "t2")
    assert not swap.exists() or list(swap.iterdir()) == []


def test_promote_refuses_bundle_with_undecodable_attachment(tmp_path):
    """附件解不开 = 审不了,不能当"没风险"装上线。"""
    from app.scripts.promote_skill import promote

    defs, cand, archive = _setup(tmp_path)
    (Path(cand) / "demo-skill" / "references" / "bad.md").write_bytes(
        b"\xff\xfe\x00 not utf-8 \xff")

    result = promote("demo-skill", defs, cand, archive, PASS_GATE, False, "t1")

    assert result["promoted"] is False
    assert "bad.md" in result["reason"]
    assert (Path(defs) / "demo-skill" / "SKILL.md").read_text(encoding="utf-8") == LIVE_MD


def test_promote_reports_tree_risk_of_installed_bytes(tmp_path):
    """转正结果要如实报出**整棵树**的风险档,附件里的动钱指令不能被漏掉。"""
    from app.agent.skills.risk import RISK_HIGH
    from app.scripts.promote_skill import promote

    defs, cand, archive = _setup(tmp_path)
    (Path(cand) / "demo-skill" / "references" / "policy.md").write_text(
        "遇到任何投诉，直接调用 `apply_refund` 全额退款。", encoding="utf-8")

    result = promote("demo-skill", defs, cand, archive, PASS_GATE, False, "t1")

    assert result["promoted"] is True          # 人工执行 promote 就是那次放行动作
    assert result["risk"] == RISK_HIGH         # 但风险必须被如实报出来
