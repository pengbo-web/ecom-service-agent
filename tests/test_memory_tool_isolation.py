"""recall_user_memory 串户回归:多会话并存时,工具必须读"当前轮 agent"的记忆。

终审发现的 pre-existing 隐患:模块级全局 _memory_manager 只在 EcomAgent.__init__
注入一次,SessionManager 缓存多 agent 并存时恒指向"最后构造的 agent",A 用户
会话里 recall 可能读到 B 用户的长期记忆。修复 = ContextVar + chat() 每轮刷新
(与 bargain.set_current_session 同模式)。
"""

from datetime import datetime

from app.agent.memory.long_term import MemoryFact
from app.agent.memory.manager import MemoryManager
from app.agent.tools.memory_tool import recall_user_memory, set_memory_manager


def _manager_with_fact(tmp_path, user_id: str, fact: str) -> MemoryManager:
    m = MemoryManager(client=None, model="test", user_id=user_id,
                      memory_dir=str(tmp_path / "mem"), memory_enabled=True)
    m.ltm.add_facts([MemoryFact(content=fact, category="preference",
                                created_at=datetime.now().isoformat())])
    return m


def test_recall_reads_current_turn_manager_not_last_constructed(tmp_path):
    """复现串户:先后注入 A、B 的 manager 后,恢复注入 A 再 recall,必须读到 A。"""
    ma = _manager_with_fact(tmp_path, "user_a", "A 喜欢红色运动鞋")
    mb = _manager_with_fact(tmp_path, "user_b", "B 是钻石会员")

    # 模拟 SessionManager 场景:B 的 agent 后构造(后注入)
    set_memory_manager(ma)
    set_memory_manager(mb)

    # A 的会话轮到来:按修复后的语义,该轮开始时会重新注入 A(chat() 每轮刷新)
    set_memory_manager(ma)
    result = recall_user_memory()
    contents = [f["content"] for f in result["long_term_facts"]]
    assert "A 喜欢红色运动鞋" in contents
    assert "B 是钻石会员" not in contents

    # B 的会话轮:同样只读到 B
    set_memory_manager(mb)
    result = recall_user_memory()
    contents = [f["content"] for f in result["long_term_facts"]]
    assert "B 是钻石会员" in contents
    assert "A 喜欢红色运动鞋" not in contents


def test_chat_refreshes_memory_manager_each_turn(tmp_path, monkeypatch):
    """关键回归:EcomAgent.chat() 每轮必须刷新注入自己的 memory_manager
    (光靠 __init__ 注入一次,在多 agent 并存时就是串户根因)。"""
    from app.agent.chat import EcomAgent
    from app.config.settings import settings

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))

    agent_a = EcomAgent(session_path=str(tmp_path / "a.json"),
                        session_id="sa", user_id="user_a")
    agent_b = EcomAgent(session_path=str(tmp_path / "b.json"),
                        session_id="sb", user_id="user_b")   # 后构造,覆盖全局注入

    # 驱动 A 的一轮 chat(fake:react 直接返回文本,不触网)
    monkeypatch.setattr(agent_a, "_react_loop", lambda: '{"intent":"other","confidence":0.9,"reply":"ok","requires_human":false}')
    monkeypatch.setattr(agent_a.memory_manager, "update_short_term", lambda *_: None)
    agent_a.chat("你好")

    # A 的轮结束后,工具侧看到的必须是 A 的 manager
    result = recall_user_memory()
    assert result["success"] is True
    # user_id 隔离:manager 归属 user_a(通过 ltm.user_id 断言)
    from app.agent.tools.memory_tool import _current_manager
    assert _current_manager() is agent_a.memory_manager
    assert _current_manager() is not agent_b.memory_manager
