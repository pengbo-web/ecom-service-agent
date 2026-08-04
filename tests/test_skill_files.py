"""渐进式披露:技能目录可带参考文件,模型用 read_skill_file 按需读。

安全底线:rel_path 来自模型输出,必须防目录穿越;只读文本,绝不执行。
"""

from app.agent.skills.loader import MAX_SKILL_FILE_CHARS, SkillManager

SKILL_MD = """---
name: demo-skill
description: 演示技能。适用关键词：演示。
---
第一步：调用 `query_order`。详见 references/policy.md。
"""


def _mgr(tmp_path, files: dict | None = None):
    d = tmp_path / "definitions" / "demo-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    for rel, content in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return SkillManager(skills_dir=str(tmp_path / "definitions"), enabled=True)


# ---------- list_skill_files ----------

def test_lists_bundled_files_excluding_skill_md(tmp_path):
    mgr = _mgr(tmp_path, {"references/policy.md": "政策正文", "templates/reply.txt": "模板"})
    files = mgr.list_skill_files("demo-skill")
    assert files == ["references/policy.md", "templates/reply.txt"]


def test_lists_empty_when_no_bundle(tmp_path):
    assert _mgr(tmp_path).list_skill_files("demo-skill") == []


def test_lists_empty_for_unknown_skill(tmp_path):
    assert _mgr(tmp_path).list_skill_files("nope") == []


# ---------- read_skill_file ----------

def test_reads_bundled_file(tmp_path):
    mgr = _mgr(tmp_path, {"references/policy.md": "七天无理由需商品完好"})
    r = mgr.read_skill_file("demo-skill", "references/policy.md")
    assert r["success"] is True
    assert r["skill_name"] == "demo-skill"
    assert "七天无理由" in r["content"]
    assert r["truncated"] is False


def test_read_truncates_huge_file(tmp_path):
    mgr = _mgr(tmp_path, {"references/big.md": "长" * (MAX_SKILL_FILE_CHARS + 500)})
    r = mgr.read_skill_file("demo-skill", "references/big.md")
    assert r["success"] is True
    assert len(r["content"]) == MAX_SKILL_FILE_CHARS
    assert r["truncated"] is True


def test_read_rejects_skill_md_itself(tmp_path):
    """SKILL.md 由 load_skill 提供,不走这个工具(避免重复灌上下文)。"""
    mgr = _mgr(tmp_path)
    assert mgr.read_skill_file("demo-skill", "SKILL.md")["success"] is False


def test_read_rejects_missing_file(tmp_path):
    mgr = _mgr(tmp_path)
    r = mgr.read_skill_file("demo-skill", "references/nope.md")
    assert r["success"] is False
    assert "不存在" in r["error"]


def test_read_rejects_path_traversal(tmp_path):
    """rel_path 来自模型输出:../ 逃出技能目录必须被拒。"""
    mgr = _mgr(tmp_path)
    outside = tmp_path / "definitions" / "secret.md"
    outside.write_text("机密", encoding="utf-8")
    for bad in ("../secret.md", "references/../../secret.md", "./../secret.md"):
        r = mgr.read_skill_file("demo-skill", bad)
        assert r["success"] is False, bad
        assert "机密" not in str(r)


def test_read_rejects_absolute_path(tmp_path):
    mgr = _mgr(tmp_path)
    r = mgr.read_skill_file("demo-skill", str(tmp_path / "definitions" / "demo-skill" / "SKILL.md"))
    assert r["success"] is False


def test_read_unknown_skill(tmp_path):
    assert _mgr(tmp_path).read_skill_file("nope", "a.md")["success"] is False


# ---------- load_skill 告知可读文件 ----------

def test_load_skill_lists_available_files(tmp_path):
    mgr = _mgr(tmp_path, {"references/policy.md": "政策"})
    instructions = mgr.load_skill("demo-skill")["instructions"]
    assert "references/policy.md" in instructions
    assert "read_skill_file" in instructions


def test_load_skill_without_bundle_adds_nothing(tmp_path):
    mgr = _mgr(tmp_path)
    instructions = mgr.load_skill("demo-skill")["instructions"]
    assert "read_skill_file" not in instructions


# ---------- 工具层 ----------

def test_tool_reads_through_injected_manager(tmp_path):
    from app.agent.tools.skill_tool import read_skill_file, set_skill_manager

    mgr = _mgr(tmp_path, {"references/policy.md": "政策正文"})
    set_skill_manager(mgr)
    r = read_skill_file("demo-skill", "references/policy.md")
    assert r["success"] is True
    assert "政策正文" in r["content"]


def test_tool_registered_in_registry():
    from app.agent.tools.registry import TOOL_DEFINITIONS, _TOOL_MAP

    assert "read_skill_file" in _TOOL_MAP
    names = {d["function"]["name"] for d in TOOL_DEFINITIONS}
    assert "read_skill_file" in names


def test_tool_in_all_profiles():
    from app.multi_agent.agents import AGENT_CONFIGS

    for key, cfg in AGENT_CONFIGS.items():
        assert "read_skill_file" in cfg["tools"], key
