"""Skill 版本身份:目录自带版本、内容指纹、转正递增、轨迹记加载那一刻的值。"""

import shutil

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
    src = tmp_path / "a"; src.mkdir()
    V.bump_version(src); V.bump_version(src)          # -> 3
    dst = tmp_path / "b"
    shutil.copytree(src, dst)
    assert V.read_version(dst) == 3


# ---------- finding 1:内容指纹(候选目录从不带 .version,版本号分不开时的补救) ----------

def test_fingerprint_missing_dir_is_unknown(tmp_path):
    assert V.fingerprint_skill_dir(tmp_path / "nope") == V.UNKNOWN_FINGERPRINT


def test_fingerprint_stable_for_identical_content(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    (a / "SKILL.md").write_text("---\nname: x\ndescription: 测试。\n---\n\n正文", encoding="utf-8")
    b = tmp_path / "b"; b.mkdir()
    (b / "SKILL.md").write_text("---\nname: x\ndescription: 测试。\n---\n\n正文", encoding="utf-8")
    assert V.fingerprint_skill_dir(a) == V.fingerprint_skill_dir(b)
    assert V.fingerprint_skill_dir(a) != V.UNKNOWN_FINGERPRINT


def test_fingerprint_differs_for_different_content(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    (a / "SKILL.md").write_text("---\nname: x\ndescription: 测试。\n---\n\n候选A", encoding="utf-8")
    b = tmp_path / "b"; b.mkdir()
    (b / "SKILL.md").write_text("---\nname: x\ndescription: 测试。\n---\n\n候选B", encoding="utf-8")
    assert V.fingerprint_skill_dir(a) != V.fingerprint_skill_dir(b)


def test_fingerprint_ignores_the_version_file_itself(tmp_path):
    """live 目录带 .version、候选目录不带——同一份正文必须算出同一个指纹,
    否则"live 与 canary 若内容相同应可比较"这条约束就不成立了。"""
    live = tmp_path / "live"; live.mkdir()
    (live / "SKILL.md").write_text("---\nname: x\ndescription: 测试。\n---\n\n正文", encoding="utf-8")
    V.bump_version(live)   # live 目录写了 .version

    cand = tmp_path / "cand"; cand.mkdir()
    (cand / "SKILL.md").write_text("---\nname: x\ndescription: 测试。\n---\n\n正文", encoding="utf-8")
    # 候选没有 .version(从不由任何创建路径打标)

    assert V.fingerprint_skill_dir(live) == V.fingerprint_skill_dir(cand)


def test_candidates_without_version_are_distinguished_by_fingerprint(tmp_path):
    """复现 + 修复证明:两个候选都读 version=1(问题本身),但指纹必须不同
    (修法),足以把两批候选的轨迹分开归因。"""
    c1 = tmp_path / "cand1"; c1.mkdir()
    (c1 / "SKILL.md").write_text("---\nname: x\ndescription: 测试。\n---\n\n候选A", encoding="utf-8")
    c2 = tmp_path / "cand2"; c2.mkdir()
    (c2 / "SKILL.md").write_text("---\nname: x\ndescription: 测试。\n---\n\n候选B", encoding="utf-8")

    assert V.read_version(c1) == 1
    assert V.read_version(c2) == 1                      # 版本号确实分不开(问题重现)
    assert V.fingerprint_skill_dir(c1) != V.fingerprint_skill_dir(c2)   # 指纹能分开


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


def test_trace_records_fingerprint(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db")); d.init_schema()
    d.record_skill_trace("s1", "u1", "track-order", [], "success", skill_fingerprint="abc123")
    t = d.list_skill_traces(limit=1)[0]
    assert t["skill_fingerprint"] == "abc123"


def test_trace_fingerprint_defaults_to_unknown(tmp_path):
    """老调用方不传指纹时落 "unknown",历史行有明确标记,不瞎猜。"""
    d = Database(db_path=str(tmp_path / "t.db")); d.init_schema()
    d.record_skill_trace("s1", "u1", "track-order", [], "success")
    assert d.list_skill_traces(limit=1)[0]["skill_fingerprint"] == "unknown"


def test_migration_backfills_unknown_fingerprint_for_legacy_db(tmp_path):
    """老库(没有 skill_fingerprint 列)升级后,历史行必须落一个明确的 "unknown"
    标记,而不是 NULL、也不是假装算得出某个真实指纹。"""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE skill_traces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_id TEXT,
            skill_name TEXT NOT NULL,
            tool_calls TEXT,
            outcome TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO skill_traces (session_id, user_id, skill_name, tool_calls, "
        "outcome, created_at) VALUES ('s0','u0','track-order','[]','success',"
        "'2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    d = Database(db_path=str(db_path))
    d.init_schema()          # 触发补列迁移
    row = d.list_skill_traces(limit=1)[0]
    assert row["skill_fingerprint"] == "unknown"
    assert row["skill_version"] == 0


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


def test_load_skill_exposes_fingerprint(tmp_path):
    from app.agent.skills.loader import SkillManager
    sd = tmp_path / "demo"; sd.mkdir()
    (sd / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 测试用。适用关键词:测试。\n---\n\n# x\n1. 用 `search_knowledge` 查。\n",
        encoding="utf-8")
    m = SkillManager(skills_dir=str(tmp_path))
    r = m.load_skill("demo")
    assert r["success"] is True
    assert r["skill_fingerprint"] == V.fingerprint_skill_dir(sd)
    assert r["skill_fingerprint"] != V.UNKNOWN_FINGERPRINT


# ---------- finding 4:落库记的是加载那一刻的值,不是落库时现读磁盘 ----------

def test_trace_carries_load_time_values_not_disk_state_at_write_time(tmp_path):
    """加载之后、落库之前磁盘发生了变化(版本递增 + 内容改写,相当于中途转正)——
    落库记的必须仍是加载那一刻带走的版本号与指纹,而不是落库那一刻重新现读磁盘
    算出来的值。"""
    from app.agent.skills.execution_trace import SkillTurn
    from app.agent.skills.loader import SkillManager

    sd = tmp_path / "demo"; sd.mkdir()
    (sd / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 测试用。适用关键词:测试。\n---\n\n# x\n1. 用 `search_knowledge` 查。\n",
        encoding="utf-8")

    mgr = SkillManager(skills_dir=str(tmp_path))
    loaded = mgr.load_skill("demo")
    assert loaded["success"] is True
    assert loaded["version"] == 1
    fp_at_load = loaded["skill_fingerprint"]

    turn = SkillTurn()
    turn.note_preloaded("demo", loaded["variant"])
    turn.set_version(loaded["version"])
    turn.set_fingerprint(loaded["skill_fingerprint"])

    # 加载完成之后,磁盘上的版本与内容都变了(模拟"服务这一轮期间发生了转正")
    V.bump_version(sd)                                    # -> 2
    (sd / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 测试用(已改)。适用关键词:测试。\n---\n\n"
        "# x\n1. 用 `search_knowledge` 查。\n",
        encoding="utf-8")

    d = Database(db_path=str(tmp_path / "t.db")); d.init_schema()
    d.record_skill_trace(
        "s1", "u1", turn.skill_name, turn.tool_calls, "success",
        variant=turn.variant, skill_version=turn.skill_version,
        skill_fingerprint=turn.skill_fingerprint,
    )
    row = d.list_skill_traces(limit=1)[0]

    assert row["skill_version"] == 1                      # 不是磁盘现在的 2
    assert row["skill_fingerprint"] == fp_at_load          # 不是磁盘现在改过内容后的指纹
    assert row["skill_fingerprint"] != V.fingerprint_skill_dir(sd)   # 佐证磁盘确实变了


# ---------- finding 2:真实驱动 promote(),而不是手工往库里塞版本号 ----------

def _write_candidate(candidates_dir, marker: str) -> None:
    cdir = candidates_dir / "track-order"
    if cdir.exists():
        shutil.rmtree(cdir)
    cdir.mkdir(parents=True)
    (cdir / "SKILL.md").write_text(
        "---\nname: track-order\ndescription: 查物流。适用关键词:物流。\n---\n\n"
        f"# 流程\n1. 用 `query_order` 核对订单。({marker})\n",
        encoding="utf-8")


def test_ab_attribution_survives_two_promotions(tmp_path):
    """本任务存在的理由:真实驱动 promote_skill.promote() 两次,断言版本号
    单调递增、两批内容可分开归因——而不是像旧版本那样手工把 1/2 塞进库里
    (那样即使 promote() 的编号退化成"每次都钉死同一个数"的原始 bug,断言
    照样通过)。falsification 见任务报告:临时把 promote() 改回"装完之后
    对新装目录 bump"这个原始 bug,本测试必须失败;改回来后必须通过。
    """
    from app.agent.skills.loader import SkillManager
    from app.scripts.promote_skill import promote

    definitions = tmp_path / "definitions"
    candidates = definitions / "_candidates"
    archive = definitions / "_archive"
    definitions.mkdir(parents=True)

    _write_candidate(candidates, "第一批")
    r1 = promote("track-order", str(definitions), str(candidates), str(archive),
                 gate_result=None, force=True, timestamp="20260101-000000")
    assert r1["promoted"] is True
    live_dir = definitions / "track-order"
    v1 = V.read_version(live_dir)

    loaded1 = SkillManager(skills_dir=str(definitions)).load_skill("track-order")
    assert loaded1["version"] == v1

    _write_candidate(candidates, "第二批")
    r2 = promote("track-order", str(definitions), str(candidates), str(archive),
                 gate_result=None, force=True, timestamp="20260101-000001")
    assert r2["promoted"] is True
    v2 = V.read_version(live_dir)

    loaded2 = SkillManager(skills_dir=str(definitions)).load_skill("track-order")
    assert loaded2["version"] == v2

    # 单调递增而非钉死同一个数——原始 bug 会让这一句失败
    assert v2 > v1

    # 用两次真实加载拿到的版本号模拟落库,断言按版本过滤能把两批分开归因
    d = Database(db_path=str(tmp_path / "t.db")); d.init_schema()
    for _ in range(3):
        d.record_skill_trace("s", "u", "track-order", [], "tool_error", skill_version=v1)
    for _ in range(2):
        d.record_skill_trace("s", "u", "track-order", [], "success", skill_version=v2)

    traces = d.list_skill_traces(skill_name="track-order", limit=99)
    group1 = [t for t in traces if t["skill_version"] == v1]
    group2 = [t for t in traces if t["skill_version"] == v2]
    assert len(group1) == 3 and all(t["outcome"] == "tool_error" for t in group1)
    assert len(group2) == 2 and all(t["outcome"] == "success" for t in group2)
