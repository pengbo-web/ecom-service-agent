"""save_user_memory:显式记忆即时写。全离线(不触网——add_facts/save 纯本地)。"""

from datetime import datetime

from app.agent.memory.manager import MemoryManager
from app.agent.tools.memory_tool import recall_user_memory, save_user_memory, set_memory_manager
from app.agent.tools.registry import TOOL_DEFINITIONS, execute_tool
from app.multi_agent.agents import AGENT_CONFIGS
from app.prompts.agents import AFTERSALE_PROMPT, MIDSALE_PROMPT, PRESALE_PROMPT


def _manager(tmp_path, user_id="u1"):
    m = MemoryManager(client=None, model="test", user_id=user_id,
                      memory_dir=str(tmp_path / "mem"), memory_enabled=True)
    set_memory_manager(m)
    return m


def test_save_persists_and_is_immediately_recallable(tmp_path):
    m = _manager(tmp_path)
    r = save_user_memory("用户偏好红色衣服", category="preference")
    assert r["success"] is True and r.get("already_known") is not True
    # 即时可召回(工具读路径)
    recalled = recall_user_memory()
    assert any("红色衣服" in f["content"] for f in recalled["long_term_facts"])
    # 已持久化:新 manager 重载还在(跨会话生效的本质)
    m2 = MemoryManager(client=None, model="test", user_id="u1",
                       memory_dir=str(tmp_path / "mem"), memory_enabled=True)
    assert any("红色衣服" in f.content for f in m2.ltm.facts)
    # FTS 同步:关键词召回命中
    assert any("红色衣服" in c for c in m2.ltm.recall("红色"))


def test_duplicate_content_reports_already_known(tmp_path):
    _manager(tmp_path)
    save_user_memory("用户是钻石会员", category="identity")
    r = save_user_memory("用户是钻石会员", category="identity")
    assert r["success"] is True and r["already_known"] is True


def test_invalid_category_falls_back_to_other(tmp_path):
    m = _manager(tmp_path)
    save_user_memory("用户常在深夜下单", category="不存在的类别")
    assert m.ltm.facts[-1].category == "other"


def test_empty_content_rejected(tmp_path):
    _manager(tmp_path)
    r = save_user_memory("   ")
    assert r["success"] is False


def test_long_content_truncated_to_200(tmp_path):
    m = _manager(tmp_path)
    save_user_memory("长" * 300, category="other")
    assert len(m.ltm.facts[-1].content) == 200


def test_no_manager_returns_error_not_raise():
    set_memory_manager(None)
    r = save_user_memory("x")
    assert r["success"] is False


def test_registered_in_registry_and_common_tools(tmp_path):
    names = {d["function"]["name"] for d in TOOL_DEFINITIONS}
    assert "save_user_memory" in names
    for cfg in AGENT_CONFIGS.values():
        assert "save_user_memory" in cfg["tools"]      # 三域公共工具
    _manager(tmp_path)
    out = execute_tool("save_user_memory", {"content": "用户偏好蓝色", "category": "preference"})
    assert '"success": true' in out.lower() or "true" in out.lower()


def test_prompts_carry_usage_and_negative_guidance():
    for p in (PRESALE_PROMPT, MIDSALE_PROMPT, AFTERSALE_PROMPT):
        assert "save_user_memory" in p
        assert "不要记录" in p          # 负面约束必须在


def test_new_fact_at_max_facts_boundary_not_misreported(tmp_path, monkeypatch):
    """评审抓的边界:facts 满 max_facts 时写入真正的新事实——
    长度比较会误报 already_known,必须用 add_facts 的真实新增数判定。"""
    m = _manager(tmp_path)
    m.ltm.max_facts = 5
    for i in range(5):
        save_user_memory(f"事实{i}", category="other")
    assert len(m.ltm.facts) == 5

    r = save_user_memory("满员后的全新事实", category="preference")
    assert r["success"] is True
    assert r["already_known"] is False           # 修复前误报 True
    contents = [f.content for f in m.ltm.facts]
    assert "满员后的全新事实" in contents        # 真写入了
    assert "事实0" not in contents               # 最老一条被淘汰
    assert len(m.ltm.facts) == 5
