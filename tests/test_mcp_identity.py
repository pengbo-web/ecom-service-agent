"""MCP 身份透传:converter 过滤 + ToolManager 注入 + server wrapper set 上下文。全离线。"""

from app.mcp_client.converter import mcp_tools_to_openai


class _FakeTool:
    def __init__(self, name, desc, schema):
        self.name = name; self.description = desc; self.inputSchema = schema


def test_converter_stripsctx_user_id():
    schema = {"type": "object",
              "properties": {"order_id": {"type": "string"}, "ctx_user_id": {"type": "string"}},
              "required": ["order_id", "ctx_user_id"]}
    out = mcp_tools_to_openai([_FakeTool("query_order", "查单", schema)])
    props = out[0]["function"]["parameters"]["properties"]
    req = out[0]["function"]["parameters"].get("required", [])
    assert "ctx_user_id" not in props        # 模型看不到
    assert "ctx_user_id" not in req
    assert "order_id" in props                 # 业务参数保留


def test_toolmanager_injects_current_user_into_mcp_call(monkeypatch):
    from app.agent.tools.manager import ToolManager
    from app.agent.runtime_context import set_current_user

    tm = ToolManager.__new__(ToolManager)      # 绕过 __init__ 的真连接
    tm._tool_source = {"query_order": "mcp"}
    tm._OFFLOAD_EXEMPT = set()
    captured = {}

    class _FakeMcp:
        def call_tool(self, name, arguments):
            captured["name"] = name; captured["args"] = dict(arguments)
            return '{"success": true}'
    tm._mcp_client = _FakeMcp()
    monkeypatch.setattr(tm, "_maybe_offload", lambda n, r: r)

    set_current_user("alice")
    tm.execute_tool("query_order", {"order_id": "O1"})
    assert captured["args"]["order_id"] == "O1"
    assert captured["args"]["ctx_user_id"] == "alice"     # 注入了当前用户
    set_current_user(None)


def test_toolmanager_injects_empty_when_no_user(monkeypatch):
    from app.agent.tools.manager import ToolManager
    from app.agent.runtime_context import set_current_user
    tm = ToolManager.__new__(ToolManager)
    tm._tool_source = {"query_order": "mcp"}; tm._OFFLOAD_EXEMPT = set()
    captured = {}
    class _FakeMcp:
        def call_tool(self, name, arguments): captured["args"] = dict(arguments); return "{}"
    tm._mcp_client = _FakeMcp()
    monkeypatch.setattr(tm, "_maybe_offload", lambda n, r: r)
    set_current_user(None)
    tm.execute_tool("query_order", {"order_id": "O1"})
    assert captured["args"]["ctx_user_id"] == ""          # 无用户→空串(server 端 fail-closed)


def test_local_tools_not_injected(monkeypatch):
    """本地工具不注入 ctx_user_id(它们直接读 ContextVar)。"""
    from app.agent.tools.manager import ToolManager
    tm = ToolManager.__new__(ToolManager)
    tm._tool_source = {"list_user_orders": "local"}; tm._OFFLOAD_EXEMPT = set()
    monkeypatch.setattr(tm, "_maybe_offload", lambda n, r: r)
    import app.agent.tools.manager as mod
    seen = {}
    monkeypatch.setattr(mod, "local_execute_tool", lambda n, a: seen.setdefault("args", dict(a)) or "{}")
    tm.execute_tool("list_user_orders", {})
    assert "ctx_user_id" not in seen["args"]              # 本地不注入
