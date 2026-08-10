"""买卖隔离的 skill 那一层:卖家 skill 绝不进买家会话,反之亦然。

背景:工具早就隔离了(各画像独立 ToolManager,tests/test_seller_profiles.py 从
注册表派生校验),但 **skill 没有**——`SkillManager` 是引擎上的同一个实例,买家
画像与卖家画像共用,`build_catalog_prompt()` 无条件列全部 skill,而两侧画像的
工具集里都有 `load_skill`。所以在加 actor 归属之前,一份写给店主的营销 skill 会
出现在买家会话的技能目录里、并能被买家侧加载出来:工具确实调不动(买家
ToolManager 里没有 find_opportunities),但**运营指令文本会原样进买家上下文**——
商机口径、催付款话术、优惠策略全在里面。

这份测试钉的就是那一层。
"""

from __future__ import annotations

import pytest

from app.agent.runtime_context import (ACTOR_BUYER, ACTOR_SELLER,
                                       get_current_actor, set_current_actor)
from app.agent.skills import SkillManager


@pytest.fixture()
def manager():
    return SkillManager()


@pytest.fixture(autouse=True)
def _reset_actor():
    """actor 是 contextvar,测试之间必须复位,否则互相污染(这类污染刚在
    tests/test_memory_tool_isolation.py 上真实发生过一次)。"""
    yield
    set_current_actor(None)


def _write_skill(root, name, actor_line=""):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试用技能。适用关键词：测试。\n{actor_line}---\n\n正文\n",
        encoding="utf-8")


def test_default_actor_is_buyer_when_unset():
    """未设置 actor 时按 buyer 处理——保守方向是"少看见",不是"看见全部"。"""
    set_current_actor(None)
    assert get_current_actor() == ACTOR_BUYER


def test_seller_skill_absent_from_buyer_catalog(manager):
    set_current_actor(ACTOR_BUYER)
    names = {c["name"] for c in manager.get_catalog()}
    assert "draft-outreach-campaign" not in names
    assert "process-return" in names          # 买家自己的 skill 不受影响


def test_buyer_skill_absent_from_seller_catalog(manager):
    set_current_actor(ACTOR_SELLER)
    names = {c["name"] for c in manager.get_catalog()}
    assert "draft-outreach-campaign" in names
    assert "process-return" not in names


def test_catalog_prompt_never_leaks_cross_actor_skill_names(manager):
    """技能名本身就是信息:买家会话的 system prompt 里不能出现营销技能名。"""
    set_current_actor(ACTOR_BUYER)
    assert "draft-outreach-campaign" not in manager.build_catalog_prompt()
    set_current_actor(ACTOR_SELLER)
    assert "process-return" not in manager.build_catalog_prompt()


def test_load_skill_refuses_cross_actor(manager):
    """目录里不列 ≠ 点名要不到。模型可能从历史消息/注入文本里拿到技能名,
    所以过滤必须落在 load_skill 里,不能只靠 catalog 少列一行。"""
    set_current_actor(ACTOR_BUYER)
    r = manager.load_skill("draft-outreach-campaign")
    assert r["success"] is False

    set_current_actor(ACTOR_SELLER)
    r2 = manager.load_skill("process-return")
    assert r2["success"] is False


def test_cross_actor_denial_leaks_no_skill_name(manager):
    """拒绝话术必须与"不存在"完全一致:回一句"这是卖家技能"等于告诉买家侧
    会话"本店有一套营销技能"(与 ensure_active 对别人的会话按未知处理、
    不回 403 是同一条零信息泄露口径)。"""
    set_current_actor(ACTOR_BUYER)
    denied = manager.load_skill("draft-outreach-campaign")["error"]
    missing = manager.load_skill("skill-that-does-not-exist")["error"]
    assert "draft-outreach-campaign" not in denied.split("，可用技能")[1]
    # 两句话的结构一致:都是"未找到 + 可用技能清单",清单内容也一样
    assert denied.split("，可用技能")[1] == missing.split("，可用技能")[1]


def test_frontmatter_without_actor_defaults_to_buyer(tmp_path):
    """既有三份 skill 都不带 actor 字段,默认必须是 buyer,行为逐字节不变。"""
    _write_skill(tmp_path, "legacy-skill")
    m = SkillManager(skills_dir=str(tmp_path))
    assert m._skills["legacy-skill"].actor == ACTOR_BUYER


def test_unknown_actor_value_falls_back_to_buyer(tmp_path):
    """写错 actor 值(如 actor: b2b)按 buyer 处理:那会被店主立刻发现
    (问它却说没这个技能);反方向的"认不出就当卖家"则会让 skill 悄悄对买家
    隐身、对店主可见,错得更难察觉。"""
    _write_skill(tmp_path, "typo-skill", actor_line="actor: b2b\n")
    m = SkillManager(skills_dir=str(tmp_path))
    assert m._skills["typo-skill"].actor == ACTOR_BUYER


def test_actor_value_is_case_insensitive(tmp_path):
    _write_skill(tmp_path, "shouty-skill", actor_line="actor: SELLER\n")
    m = SkillManager(skills_dir=str(tmp_path))
    assert m._skills["shouty-skill"].actor == ACTOR_SELLER
