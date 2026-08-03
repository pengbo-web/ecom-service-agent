"""G4 落地：转正 CLI（校验→门禁→备份→写正式目录）与回滚。

强调：这是全仓库唯一允许写 definitions/ 正式目录的自动化路径。
"""

from pathlib import Path

from app.scripts.promote_skill import (
    backup_current,
    list_candidates,
    promote,
    rollback,
)

LIVE_MD = """---
name: process-return
description: 退货处理流程(现行版)。
---
现行正文。
"""

CANDIDATE_MD = """---
name: process-return
description: 退货处理流程(候选改进版)。
---
第一步：调用 `query_order` 核对订单。
"""

BAD_CANDIDATE_MD = """---
name: coupon-lookup
description: 查券流程。
---
第一步：调用 `coupon_query` 查券。
"""


def _dirs(tmp_path, with_live=True, candidates=None):
    definitions = tmp_path / "definitions"
    definitions.mkdir(parents=True, exist_ok=True)
    if with_live:
        (definitions / "process-return").mkdir(parents=True, exist_ok=True)
        (definitions / "process-return" / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")

    cand_dir = definitions / "_candidates"
    for name, content in (candidates or {}).items():
        (cand_dir / name).mkdir(parents=True, exist_ok=True)
        (cand_dir / name / "SKILL.md").write_text(content, encoding="utf-8")

    archive = definitions / "_archive"
    return str(definitions), str(cand_dir), str(archive)


PASS_GATE = {"promote": True, "reason": "候选未劣化,允许转正"}
FAIL_GATE = {"promote": False, "reason": "候选劣化超过容差,拒绝转正"}


# ---------- list_candidates ----------

def test_list_candidates_reports_validation_and_kind(tmp_path):
    definitions, cand_dir, _ = _dirs(tmp_path, candidates={
        "process-return": CANDIDATE_MD, "coupon-lookup": BAD_CANDIDATE_MD})

    items = {c["name"]: c for c in list_candidates(cand_dir, definitions)}

    assert items["process-return"]["valid"] is True
    assert items["process-return"]["is_improvement"] is True     # 正式目录已有同名
    assert items["coupon-lookup"]["valid"] is False
    assert items["coupon-lookup"]["unknown_tools"] == ["coupon_query"]
    assert items["coupon-lookup"]["is_improvement"] is False     # 全新 skill


def test_list_candidates_empty_dir(tmp_path):
    definitions, cand_dir, _ = _dirs(tmp_path)
    assert list_candidates(cand_dir, definitions) == []


# ---------- backup ----------

def test_backup_current_copies_live_version(tmp_path):
    definitions, _, archive = _dirs(tmp_path)
    path = backup_current(definitions, "process-return", archive, "20260803-120000")

    assert path is not None
    assert path.read_text(encoding="utf-8") == LIVE_MD
    assert path.parts[-2] == "20260803-120000"


def test_backup_current_returns_none_for_new_skill(tmp_path):
    definitions, _, archive = _dirs(tmp_path, with_live=False)
    assert backup_current(definitions, "coupon-lookup", archive, "20260803-120000") is None


# ---------- promote ----------

def test_promote_writes_definitions_and_backs_up(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})

    result = promote("process-return", definitions, cand_dir, archive,
                     gate_result=PASS_GATE, force=False, timestamp="20260803-120000")

    assert result["promoted"] is True
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == CANDIDATE_MD          # 已生效
    backup = tmp_path / "definitions" / "_archive" / "process-return" / "20260803-120000" / "SKILL.md"
    assert backup.read_text(encoding="utf-8") == LIVE_MD             # 旧版已备份


def test_promote_blocked_by_validation(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, with_live=False,
                                           candidates={"coupon-lookup": BAD_CANDIDATE_MD})

    result = promote("coupon-lookup", definitions, cand_dir, archive,
                     gate_result=PASS_GATE, force=False, timestamp="t1")

    assert result["promoted"] is False
    assert "coupon_query" in result["reason"]
    assert not (tmp_path / "definitions" / "coupon-lookup").exists()   # 未写正式目录


def test_promote_blocked_by_gate(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})

    result = promote("process-return", definitions, cand_dir, archive,
                     gate_result=FAIL_GATE, force=False, timestamp="t1")

    assert result["promoted"] is False
    assert "劣化" in result["reason"]
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == LIVE_MD   # 正式目录未被改动


def test_promote_force_overrides_gate_but_not_validation(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={
        "process-return": CANDIDATE_MD, "coupon-lookup": BAD_CANDIDATE_MD})

    forced = promote("process-return", definitions, cand_dir, archive,
                     gate_result=FAIL_GATE, force=True, timestamp="t1")
    assert forced["promoted"] is True

    still_blocked = promote("coupon-lookup", definitions, cand_dir, archive,
                            gate_result=FAIL_GATE, force=True, timestamp="t2")
    assert still_blocked["promoted"] is False   # --force 不能绕过工具名校验


def test_promote_missing_candidate(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path)
    result = promote("nope", definitions, cand_dir, archive,
                     gate_result=PASS_GATE, force=False, timestamp="t1")
    assert result["promoted"] is False
    assert "候选不存在" in result["reason"]


# ---------- rollback ----------

def test_rollback_restores_latest_backup(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})
    promote("process-return", definitions, cand_dir, archive,
            gate_result=PASS_GATE, force=False, timestamp="20260803-120000")

    result = rollback("process-return", definitions, archive)

    assert result["rolled_back"] is True
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == LIVE_MD   # 回到现行版


def test_rollback_picks_newest_of_multiple_backups(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})
    promote("process-return", definitions, cand_dir, archive, PASS_GATE, False, "20260801-090000")
    # 第二次转正:此时现行版已是 CANDIDATE_MD,备份进 20260803
    promote("process-return", definitions, cand_dir, archive, PASS_GATE, False, "20260803-120000")

    rollback("process-return", definitions, archive)
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == CANDIDATE_MD   # 恢复的是最新备份


def test_rollback_without_backup_fails_safely(tmp_path):
    definitions, _, archive = _dirs(tmp_path)
    result = rollback("process-return", definitions, archive)
    assert result["rolled_back"] is False
    assert "无备份" in result["reason"]


def test_archive_dir_not_loaded_by_skill_manager(tmp_path):
    """铁律回归:_archive 嵌套层级保证 SkillManager 不会把备份当成活的 skill。"""
    from app.agent.skills.loader import SkillManager

    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})
    promote("process-return", definitions, cand_dir, archive, PASS_GATE, False, "t1")

    sm = SkillManager(skills_dir=definitions, enabled=True)
    assert sm.skill_names == ["process-return"]
    assert sm.skill_count == 1


# ---------- 路径安全:本模块是唯一允许写正式目录的入口,必须自守 ----------

def test_rollback_rejects_unsafe_skill_name(tmp_path):
    """rollback("..") 会把 _archive/.. 解回 definitions/,必须在动手之前拒绝。"""
    definitions, cand_dir, archive = _dirs(tmp_path)
    before = (tmp_path / "definitions" / "process-return" / "SKILL.md").read_text(encoding="utf-8")

    for bad in ("..", "../evil", "a/b", "a\\b", ""):
        result = rollback(bad, definitions, archive)
        assert result["rolled_back"] is False, bad
        assert "非法" in result["reason"], bad

    assert (tmp_path / "definitions" / "process-return" / "SKILL.md").read_text(
        encoding="utf-8") == before
    assert not (tmp_path / "SKILL.md").exists()      # 没写到 definitions/ 之外


def test_promote_rejects_unsafe_skill_name(tmp_path):
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})

    for bad in ("..", "../evil", "a/b", ""):
        result = promote(bad, definitions, cand_dir, archive,
                         gate_result=PASS_GATE, force=True, timestamp="t1")
        assert result["promoted"] is False, bad
        assert "非法" in result["reason"], bad


def test_backup_current_rejects_unsafe_skill_name(tmp_path):
    definitions, _, archive = _dirs(tmp_path)
    assert backup_current(definitions, "..", archive, "t1") is None


MISMATCHED_NAME_MD = """---
name: some-other-skill
description: 目录名与 frontmatter name 不一致。
---
第一步：调用 `query_order`。
"""


def test_promote_rejects_name_dir_mismatch(tmp_path):
    """目录名与 frontmatter name 不一致必须拒绝:否则线上会注册成另一个名字。"""
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": MISMATCHED_NAME_MD})

    result = promote("process-return", definitions, cand_dir, archive,
                     gate_result=PASS_GATE, force=True, timestamp="t1")

    assert result["promoted"] is False
    assert "不一致" in result["reason"]
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == LIVE_MD


def test_list_candidates_flags_name_dir_mismatch(tmp_path):
    definitions, cand_dir, _ = _dirs(tmp_path, candidates={"process-return": MISMATCHED_NAME_MD})
    item = list_candidates(cand_dir, definitions)[0]
    assert item["valid"] is False
    assert any("不一致" in e for e in item["errors"])


def test_rollback_ignores_rejected_archive_dirs(tmp_path):
    """rejected-* 是落败候选的存档,从未上线过,不得被当成可回滚的版本。"""
    definitions, cand_dir, archive = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})
    promote("process-return", definitions, cand_dir, archive, PASS_GATE, False, "20260801-090000")
    rej = Path(archive) / "process-return" / "rejected-99999999-999999"
    rej.mkdir(parents=True)
    (rej / "SKILL.md").write_text("REJECTED CONTENT", encoding="utf-8")

    result = rollback("process-return", definitions, archive)

    assert result["rolled_back"] is True
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == LIVE_MD     # 恢复的是真正上线过的版本


def test_list_candidates_survives_unreadable_candidate(tmp_path):
    """单个候选解不开不能让整份清单崩掉——标为不合法,其余照常列出。"""
    definitions, cand_dir, _ = _dirs(tmp_path, candidates={"process-return": CANDIDATE_MD})
    broken = Path(cand_dir) / "broken-skill"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_bytes(b"\xff\xfe\x00 not utf-8 \xff")

    items = {c["name"]: c for c in list_candidates(cand_dir, definitions)}

    assert items["process-return"]["valid"] is True          # 好的仍在
    assert items["broken-skill"]["valid"] is False           # 坏的被标记而非崩掉
    assert any("读取候选失败" in e for e in items["broken-skill"]["errors"])
