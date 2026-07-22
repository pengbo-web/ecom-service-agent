"""Phase 7a:SKILL.md frontmatter 用 yaml.safe_load,支持嵌套/列表/多行/引号,坏 YAML 不崩。"""

from app.agent.skills.loader import _parse_frontmatter, _parse_body, SkillManager


def test_flat_key_value():
    meta = _parse_frontmatter("---\nname: process-return\ndescription: 处理退货\n---\n正文")
    assert meta["name"] == "process-return"
    assert meta["description"] == "处理退货"


def test_nested_and_list():
    # 旧手写解析器会把嵌套/列表解析错;yaml.safe_load 正确
    content = (
        "---\n"
        "name: demo\n"
        "description: 演示\n"
        "requires:\n"
        "  env:\n"
        "    - OPENAI_API_KEY\n"
        "    - DB_URL\n"
        "tags: [a, b, c]\n"
        "---\n"
        "body"
    )
    meta = _parse_frontmatter(content)
    assert meta["requires"]["env"] == ["OPENAI_API_KEY", "DB_URL"]
    assert meta["tags"] == ["a", "b", "c"]


def test_multiline_block_scalar():
    content = (
        "---\n"
        "name: demo\n"
        "description: >\n"
        "  第一行\n"
        "  第二行\n"
        "---\n"
        "body"
    )
    meta = _parse_frontmatter(content)
    assert "第一行" in meta["description"] and "第二行" in meta["description"]


def test_quoted_value_with_colon():
    # 冒号在值里:旧解析器 partition 会截断,yaml 正确
    meta = _parse_frontmatter('---\nname: demo\ndescription: "时间: 9:00-18:00"\n---\nb')
    assert meta["description"] == "时间: 9:00-18:00"


def test_malformed_yaml_returns_empty():
    # 非法 YAML → {} 而非抛异常
    assert _parse_frontmatter("---\nname: [unclosed\n---\nbody") == {}


def test_no_frontmatter_returns_empty():
    assert _parse_frontmatter("没有 frontmatter 的纯正文") == {}


def test_non_mapping_returns_empty():
    # frontmatter 是列表而非映射 → {}
    assert _parse_frontmatter("---\n- a\n- b\n---\nbody") == {}


def test_body_still_extracted():
    assert _parse_body("---\nname: x\n---\n# 标题\n内容") == "# 标题\n内容"


def test_discover_skips_bad_skill(tmp_path):
    # 一个正常 skill + 一个坏 frontmatter 的 skill → 只加载正常的,不崩
    good = tmp_path / "good"; good.mkdir()
    (good / "SKILL.md").write_text("---\nname: good\ndescription: 好的\n---\n正文", encoding="utf-8")
    bad = tmp_path / "bad"; bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: [unclosed\n---\n正文", encoding="utf-8")

    mgr = SkillManager(skills_dir=str(tmp_path))
    assert mgr.skill_names == ["good"]
