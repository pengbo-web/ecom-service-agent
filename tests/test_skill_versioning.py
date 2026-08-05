"""Skill 版本身份:目录自带版本、转正递增、轨迹记加载那一刻的版本。"""

import pytest

from app.agent.skills import versioning as V
from app.db.database import Database


def test_missing_version_file_reads_as_one(tmp_path):
    assert V.read_version(tmp_path) == 1


def test_bump_creates_and_increments(tmp_path):
    assert V.bump_version(tmp_path) == 2
    assert V.read_version(tmp_path) == 2
    assert V.bump_version(tmp_path) == 3


def test_corrupt_version_file_reads_as_one(tmp_path):
    (tmp_path / V.VERSION_FILE).write_text("不是数字", encoding="utf-8")
    assert V.read_version(tmp_path) == 1


def test_version_travels_with_the_directory(tmp_path):
    """版本号放目录里,所以上传/转正/回滚的整目录搬运天然带着它走。"""
    import shutil
    src = tmp_path / "a"; src.mkdir()
    V.bump_version(src); V.bump_version(src)          # -> 3
    dst = tmp_path / "b"
    shutil.copytree(src, dst)
    assert V.read_version(dst) == 3


def test_trace_records_version(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db")); d.init_schema()
    d.record_skill_trace("s1", "u1", "track-order", [], "success", skill_version=7)
    t = d.list_skill_traces(limit=1)[0]
    assert t["skill_version"] == 7


def test_trace_version_defaults_to_unknown(tmp_path):
    """老调用方不传版本时落 0=未知,而不是假装是第 1 版。"""
    d = Database(db_path=str(tmp_path / "t.db")); d.init_schema()
    d.record_skill_trace("s1", "u1", "track-order", [], "success")
    assert d.list_skill_traces(limit=1)[0]["skill_version"] == 0


def test_load_skill_exposes_version(tmp_path):
    from app.agent.skills.loader import SkillManager
    sd = tmp_path / "demo"; sd.mkdir()
    (sd / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 测试用。适用关键词:测试。\n---\n\n# x\n1. 用 `search_knowledge` 查。\n",
        encoding="utf-8")
    V.bump_version(sd)          # -> 2
    m = SkillManager(skills_dir=str(tmp_path))
    r = m.load_skill("demo")
    assert r["success"] is True
    assert r["version"] == 2


def test_ab_attribution_survives_two_promotions(tmp_path):
    """本任务存在的理由:同一 skill 转正两次后,仍能按版本把轨迹分开归因。"""
    d = Database(db_path=str(tmp_path / "t.db")); d.init_schema()
    for _ in range(3):
        d.record_skill_trace("s", "u", "track-order", [], "tool_error", skill_version=1)
    for _ in range(2):
        d.record_skill_trace("s", "u", "track-order", [], "success", skill_version=2)
    traces = d.list_skill_traces(skill_name="track-order", limit=99)
    v1 = [t for t in traces if t["skill_version"] == 1]
    v2 = [t for t in traces if t["skill_version"] == 2]
    assert len(v1) == 3 and all(t["outcome"] == "tool_error" for t in v1)
    assert len(v2) == 2 and all(t["outcome"] == "success" for t in v2)
