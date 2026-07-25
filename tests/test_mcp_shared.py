"""MCP 共享单例:一次连接全进程复用 + 失败降级 None + reset。全离线(fake MCPClient)。"""

import app.mcp_client.shared as shared


class _FakeMCP:
    instances = 0
    def __init__(self, url):
        _FakeMCP.instances += 1
        self.url = url; self.closed = False
    def connect(self):
        return [{"type": "function", "function": {"name": "query_order", "parameters": {}}}]
    def close(self):
        self.closed = True


class _FailMCP:
    def __init__(self, url): pass
    def connect(self): raise ConnectionError("server down")
    def close(self): pass


def setup_function():
    shared.reset_shared_mcp()
    _FakeMCP.instances = 0


def test_single_connect_reused_across_calls(monkeypatch):
    monkeypatch.setattr(shared, "MCPClient", _FakeMCP)
    a = shared.get_shared_mcp_client("http://x/mcp")
    b = shared.get_shared_mcp_client("http://x/mcp")
    assert a is not None and b is not None
    assert a[0] is b[0]                       # 同一 client 实例
    assert _FakeMCP.instances == 1            # 只连一次(4 个 ToolManager 复用)
    assert a[1][0]["function"]["name"] == "query_order"


def test_connect_failure_returns_none(monkeypatch):
    monkeypatch.setattr(shared, "MCPClient", _FailMCP)
    assert shared.get_shared_mcp_client("http://x/mcp") is None
    # 失败不缓存:下次仍尝试(可能 server 起来了)
    assert shared.get_shared_mcp_client("http://x/mcp") is None


def test_reset_closes_and_reconnects(monkeypatch):
    monkeypatch.setattr(shared, "MCPClient", _FakeMCP)
    a = shared.get_shared_mcp_client("http://x/mcp")
    client = a[0]
    shared.reset_shared_mcp()
    assert client.closed is True              # reset 关旧连接
    shared.get_shared_mcp_client("http://x/mcp")
    assert _FakeMCP.instances == 2            # reset 后重连


def test_toolmanager_uses_shared_and_close_keeps_it(monkeypatch):
    import app.mcp_client.shared as sh
    from app.agent.tools.manager import ToolManager

    class _C:
        def __init__(self): self.closed = False
        def close(self): self.closed = True
        def call_tool(self, name, args): return "{}"
    fake = _C()
    tools = [{"type": "function", "function": {"name": "query_order", "parameters": {"type": "object", "properties": {}}}}]
    sh.set_shared_mcp_for_test(fake, tools)
    # 两个 ToolManager 都用同一个共享 client
    tm1 = ToolManager(use_mcp=True, mcp_server_url="test")
    tm2 = ToolManager(use_mcp=True, mcp_server_url="test")
    assert tm1._mcp_client is fake and tm2._mcp_client is fake
    assert tm1._shared_mcp is True
    tm1.close()                               # 一个会话结束
    assert fake.closed is False               # 共享 client 不被关(tm2 还在用)
    tm2.close()
    assert fake.closed is False               # 共享 client 始终由 reset/进程管
    sh.reset_shared_mcp()


def test_toolmanager_degrades_when_shared_none(monkeypatch):
    """共享连接不可用时 ToolManager 降级本地。走真实失败路径:让 shared 内部
    MCPClient.connect 抛错 → get_shared_mcp_client 返回 None → ToolManager 降级
    (比 patch get_shared_mcp_client 更稳——延迟导入 patch 目标易错,直接打 shared
    模块顶部的 MCPClient 引用必命中,且验证的是真实降级链路,不真连网络)。"""
    import app.mcp_client.shared as sh
    from app.agent.tools.manager import ToolManager

    class _FailMCP:
        def __init__(self, url): pass
        def connect(self): raise ConnectionError("server down")
        def close(self): pass

    sh.reset_shared_mcp()
    monkeypatch.setattr(sh, "MCPClient", _FailMCP)
    tm = ToolManager(use_mcp=True, mcp_server_url="http://x/mcp")
    names = {d["function"]["name"] for d in tm.tool_definitions}
    assert "list_user_orders" in names        # 降级本地工具(local 独有)
    assert tm._mcp_client is None
    sh.reset_shared_mcp()
