"""灰度路由:候选按会话哈希接管部分流量,与现行版 A/B;任何异常退回正式版。"""

import json

from app.agent.skills.canary import VARIANT_CANARY, VARIANT_LIVE, in_canary_bucket
from app.agent.skills.loader import SkillManager
from app.db import Database

LIVE_MD = """---
name: process-return
description: 退货处理(现行版)。
---
现行正文。
"""

CANDIDATE_MD = """---
name: process-return
description: 退货处理(候选版)。
---
候选正文。
"""


def _definitions(tmp_path):
    d = tmp_path / "definitions"
    (d / "process-return").mkdir(parents=True)
    (d / "process-return" / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")
    return d


def _candidate(tmp_path):
    p = tmp_path / "candidates" / "process-return" / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text(CANDIDATE_MD, encoding="utf-8")
    return p


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    return db


# ---------- in_canary_bucket ----------

def test_bucket_zero_percent_never_hits():
    assert in_canary_bucket("s", "sess-1", 0) is False


def test_bucket_hundred_percent_always_hits():
    assert in_canary_bucket("s", "sess-1", 100) is True


def test_bucket_is_deterministic_per_session():
    """同一会话反复判定结果不变(一通对话不能中途换版本)。"""
    first = in_canary_bucket("process-return", "sess-42", 50)
    for _ in range(5):
        assert in_canary_bucket("process-return", "sess-42", 50) is first


def test_bucket_splits_traffic_roughly_by_percent():
    hits = sum(1 for i in range(400) if in_canary_bucket("process-return", f"sess-{i}", 25))
    assert 50 < hits < 150   # 25% of 400 = 100,允许哈希抖动


# ---------- DB 灰度登记 ----------

def test_start_and_get_active_canary(tmp_path):
    db = _db(tmp_path)
    db.start_canary("process-return", "/path/SKILL.md", 50, "low", "canary_ab")

    row = db.get_active_canary("process-return")
    assert row["percent"] == 50
    assert row["risk"] == "low"
    assert row["policy"] == "canary_ab"
    assert row["status"] == "active"
    assert db.get_active_canary("track-order") is None


def test_start_canary_supersedes_previous(tmp_path):
    db = _db(tmp_path)
    db.start_canary("process-return", "/old.md", 10, "low", "canary_ab")
    db.start_canary("process-return", "/new.md", 50, "low", "canary_ab")

    assert db.get_active_canary("process-return")["candidate_path"] == "/new.md"
    assert len(db.list_active_canaries()) == 1   # 同 skill 同时只有一个活跃灰度


def test_finish_canary_marks_status(tmp_path):
    db = _db(tmp_path)
    db.start_canary("process-return", "/p.md", 50, "low", "canary_ab")

    assert db.finish_canary("process-return", "promoted") is True
    assert db.get_active_canary("process-return") is None
    assert db.finish_canary("process-return", "promoted") is False   # 已无活跃记录


def test_record_skill_trace_stores_variant(tmp_path):
    db = _db(tmp_path)
    db.record_skill_trace("s1", "u1", "process-return", [], "success", variant=VARIANT_CANARY)
    db.record_skill_trace("s2", "u1", "process-return", [], "success")   # 默认 live

    rows = {r["session_id"]: r for r in db.list_skill_traces()}
    assert rows["s1"]["variant"] == VARIANT_CANARY
    assert rows["s2"]["variant"] == VARIANT_LIVE


# ---------- load_skill 路由 ----------

def test_load_skill_returns_live_variant_without_canary(tmp_path, monkeypatch):
    from app.config.settings import settings

    definitions = _definitions(tmp_path)
    db = _db(tmp_path)
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_canary_enabled", True)
    monkeypatch.setattr("app.agent.tools.bargain.get_current_session", lambda: "sess-1")

    result = SkillManager(skills_dir=str(definitions), enabled=True).load_skill("process-return")

    assert result["success"] is True
    assert result["variant"] == VARIANT_LIVE
    assert "现行正文" in result["instructions"]


def test_load_skill_serves_candidate_when_session_in_bucket(tmp_path, monkeypatch):
    from app.config.settings import settings

    definitions = _definitions(tmp_path)
    candidate = _candidate(tmp_path)
    db = _db(tmp_path)
    db.start_canary("process-return", str(candidate), 100, "low", "canary_ab")   # 100% 必中
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_canary_enabled", True)
    monkeypatch.setattr("app.agent.tools.bargain.get_current_session", lambda: "sess-1")

    result = SkillManager(skills_dir=str(definitions), enabled=True).load_skill("process-return")

    assert result["variant"] == VARIANT_CANARY
    assert "候选正文" in result["instructions"]


def test_load_skill_falls_back_when_canary_disabled(tmp_path, monkeypatch):
    from app.config.settings import settings

    definitions = _definitions(tmp_path)
    candidate = _candidate(tmp_path)
    db = _db(tmp_path)
    db.start_canary("process-return", str(candidate), 100, "low", "canary_ab")
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_canary_enabled", False)   # 总开关关
    monkeypatch.setattr("app.agent.tools.bargain.get_current_session", lambda: "sess-1")

    result = SkillManager(skills_dir=str(definitions), enabled=True).load_skill("process-return")
    assert result["variant"] == VARIANT_LIVE
    assert "现行正文" in result["instructions"]


def test_load_skill_falls_back_when_candidate_file_missing(tmp_path, monkeypatch):
    from app.config.settings import settings

    definitions = _definitions(tmp_path)
    db = _db(tmp_path)
    db.start_canary("process-return", str(tmp_path / "gone" / "SKILL.md"), 100, "low", "canary_ab")
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_canary_enabled", True)
    monkeypatch.setattr("app.agent.tools.bargain.get_current_session", lambda: "sess-1")

    result = SkillManager(skills_dir=str(definitions), enabled=True).load_skill("process-return")
    assert result["variant"] == VARIANT_LIVE   # 候选文件没了 → 退回正式版,不报错


def test_load_skill_falls_back_when_db_raises(tmp_path, monkeypatch):
    from app.config.settings import settings

    definitions = _definitions(tmp_path)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.db.get_db", boom)
    monkeypatch.setattr(settings, "skill_canary_enabled", True)
    monkeypatch.setattr("app.agent.tools.bargain.get_current_session", lambda: "sess-1")

    result = SkillManager(skills_dir=str(definitions), enabled=True).load_skill("process-return")
    assert result["success"] is True            # 灰度炸了不能让 load_skill 失败
    assert result["variant"] == VARIANT_LIVE


# ---------- 灰度期附带资料必须跟着候选走(否则 A/B 在测一条错接线) ----------

def _canary_env(tmp_path, monkeypatch, live_files=None, cand_files=None):
    """搭一套"线上版与候选版各带不同附件"的环境,并把灰度开到 100%。"""
    from app.config.settings import settings

    definitions = _definitions(tmp_path)
    for rel, content in (live_files or {}).items():
        p = definitions / "process-return" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    candidate = _candidate(tmp_path)
    for rel, content in (cand_files or {}).items():
        p = candidate.parent / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    db = _db(tmp_path)
    db.start_canary("process-return", str(candidate), 100, "low", "canary_ab")
    monkeypatch.setattr("app.db.get_db", lambda: db)
    monkeypatch.setattr(settings, "skill_canary_enabled", True)
    monkeypatch.setattr("app.agent.tools.bargain.get_current_session", lambda: "sess-1")
    return SkillManager(skills_dir=str(definitions), enabled=True)


def test_canary_session_sees_candidate_attachments(tmp_path, monkeypatch):
    """灰度会话拿到的是候选正文,文件清单就必须也来自候选目录。

    否则模型看到线上的清单、读到线上的文件,候选新增的附件一读就是「文件不存在」:
    候选的 A/B 成绩被这条接线人为压低,看门狗于是把一个好候选回滚掉。
    """
    mgr = _canary_env(tmp_path, monkeypatch,
                      live_files={"references/live.md": "现行参考"},
                      cand_files={"references/cand.md": "候选参考"})

    result = mgr.load_skill("process-return")

    assert result["variant"] == VARIANT_CANARY
    assert "references/cand.md" in result["instructions"]
    assert "references/live.md" not in result["instructions"]
    assert mgr.list_skill_files("process-return") == ["references/cand.md"]


def test_canary_session_reads_candidate_attachment_content(tmp_path, monkeypatch):
    mgr = _canary_env(tmp_path, monkeypatch,
                      live_files={"references/policy.md": "现行政策"},
                      cand_files={"references/policy.md": "候选政策"})

    r = mgr.read_skill_file("process-return", "references/policy.md")

    assert r["success"] is True
    assert r["content"] == "候选政策"


def test_canary_root_still_rejects_traversal(tmp_path, monkeypatch):
    """换了根目录也不能松掉穿越防护:rel_path 依旧来自模型输出。"""
    (tmp_path / "candidates" ).mkdir(exist_ok=True)
    mgr = _canary_env(tmp_path, monkeypatch, cand_files={"references/cand.md": "候选参考"})
    secret = tmp_path / "candidates" / "secret.md"
    secret.write_text("机密", encoding="utf-8")

    for bad in ("../secret.md", "references/../../secret.md", "./../secret.md"):
        r = mgr.read_skill_file("process-return", bad)
        assert r["success"] is False, bad
        assert "机密" not in str(r), bad


def test_canary_falls_back_to_live_when_candidate_dir_gone(tmp_path, monkeypatch):
    """候选目录整个没了 → 正文与清单一起退回正式版,不能半灰半正。"""
    import shutil

    mgr = _canary_env(tmp_path, monkeypatch,
                      live_files={"references/live.md": "现行参考"},
                      cand_files={"references/cand.md": "候选参考"})
    shutil.rmtree(tmp_path / "candidates" / "process-return")

    result = mgr.load_skill("process-return")

    assert result["variant"] == VARIANT_LIVE
    assert "现行正文" in result["instructions"]
    assert "references/live.md" in result["instructions"]


# ---------- SkillTurn 记住 variant ----------

def test_skill_turn_captures_variant_from_load_result():
    from app.agent.skills.execution_trace import SkillTurn

    turn = SkillTurn()
    turn.note_tool_call("load_skill", json.dumps(
        {"success": True, "skill_name": "process-return", "instructions": "x",
         "variant": VARIANT_CANARY}, ensure_ascii=False))

    assert turn.skill_name == "process-return"
    assert turn.variant == VARIANT_CANARY


def test_skill_turn_variant_defaults_to_live():
    from app.agent.skills.execution_trace import SkillTurn

    turn = SkillTurn()
    turn.note_tool_call("load_skill", json.dumps(
        {"success": True, "skill_name": "process-return", "instructions": "x"},
        ensure_ascii=False))

    assert turn.variant == VARIANT_LIVE
