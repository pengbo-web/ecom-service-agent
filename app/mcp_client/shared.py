"""进程级共享 MCP 连接:全进程一个 MCPClient,所有 ToolManager 复用。

原本每个 ToolManager(引擎+每个画像)各建一个 MCPClient+后台线程+连接,
一会话 4 次握手。共享后全进程一次。仿 session store 的 get_/set_ 单例模式。
"""

from __future__ import annotations

import threading

from app.mcp_client.client import MCPClient

_lock = threading.Lock()
_client: MCPClient | None = None
_tools: list | None = None
_url: str | None = None


def get_shared_mcp_client(server_url: str):
    """取全进程共享的 (client, tools);首次连接,失败返回 None(调用方降级)。"""
    global _client, _tools, _url
    with _lock:
        if _client is not None and _url == server_url:
            return _client, _tools
        try:
            c = MCPClient(server_url)
            tools = c.connect()
        except Exception:
            return None
        _client, _tools, _url = c, tools, server_url
        return _client, _tools


def reset_shared_mcp() -> None:
    """关闭并清空共享连接(测试/强制重连用)。"""
    global _client, _tools, _url
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = _tools = _url = None


def set_shared_mcp_for_test(client, tools) -> None:
    """测试注入(绕过真实连接)。"""
    global _client, _tools, _url
    _client, _tools, _url = client, tools, "test"
