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
