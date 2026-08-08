"""W1 服务化 L2:两个不同会话的 Agent 构造应复用同一份进程级 LLM 传输层
(这正是"第二个及之后的新会话"构造耗时从 ~0.86s 降到接近 0 的机制——见
app/observability/http_pool.py),但绝不共享任何与用户/会话相关的状态:
记忆(用户数据)、消息历史、ToolManager 都必须仍然各自独立。
"""

from app.api.session_manager import SessionManager
from app.observability import http_pool


def _underlying_httpx_client(agent):
    client = agent.engine.client
    primary = getattr(client, "_primary", client)   # resilience_enabled 时包了一层 ResilientChatClient
    return primary._client


def test_two_new_sessions_share_transport_but_not_user_state(tmp_path, monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "memory"))

    http_pool.reset_http_pool()
    mgr = SessionManager(base_dir=str(tmp_path / "api"))
    a = mgr.get_or_create("session-a", "user-a")
    b = mgr.get_or_create("session-b", "user-b")

    # 共享:底层 LLM 传输层(无状态,只负责连接池/SSL)
    assert _underlying_httpx_client(a) is _underlying_httpx_client(b)

    # 不共享:一切携带用户/会话身份或数据的对象
    assert a.memory_manager is not b.memory_manager
    assert a.memory_manager.ltm.user_id == "user-a"
    assert b.memory_manager.ltm.user_id == "user-b"
    assert a.engine.raw_messages is not b.engine.raw_messages
    assert a.engine.tool_manager is not b.engine.tool_manager
    assert a.engine is not b.engine
    assert a.user_id != b.user_id

    # 往 a 的记忆/历史里写点东西,确认不会跑到 b 上
    a.engine.raw_messages.append({"role": "user", "content": "only for a"})
    assert b.engine.raw_messages == []
