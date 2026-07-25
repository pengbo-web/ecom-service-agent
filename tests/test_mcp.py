"""第 4 期 MCP 集成 smoke(显式 opt-in)。

这是**真 server 集成测试**,默认不跑——需显式开启 + 手动起 server:
  1. 启动 MCP Server:  python mcp_server/server.py
  2. 开 env 跑:        RUN_MCP_INTEGRATION=1 pytest tests/test_mcp.py -v

为什么 opt-in 而非端口探测:MCP 现已常开(server 常驻),端口探测会让这些
集成测试在常规 `pytest` 里被动真跑,拖慢且依赖外部 server。显式 env 只在
"我确实要验集成"时跑。连接/schema/调用/降级的**逻辑**已由 tests/test_mcp_shared.py
+ tests/test_mcp_identity.py 做了 hermetic 覆盖(不需真 server),本文件只补
"对着真 server 端到端连一次"的 smoke。真 LLM 端到端已由手动/浏览器冒烟覆盖,
不放进自动化(脆且慢),故本文件不含 agent.chat 用例。
"""

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_MCP_INTEGRATION"),
    reason="MCP 集成测试需显式开启:RUN_MCP_INTEGRATION=1 且 mcp_server 已在 :9123 运行",
)

from app.mcp_client import MCPClient  # noqa: E402
from app.agent.tools.manager import ToolManager  # noqa: E402

MCP_URL = "http://127.0.0.1:9123/mcp"

# server 现暴露 5 个工具(query_order/query_product/query_logistics/apply_refund/search_knowledge)
EXPECTED_TOOLS = {"query_order", "query_product", "query_logistics", "apply_refund", "search_knowledge"}


def test_mcp_connection_discovers_expected_tools():
    client = MCPClient(MCP_URL)
    try:
        tools = client.connect()
        names = {t["function"]["name"] for t in tools}
        assert names == EXPECTED_TOOLS       # 期望随 server 实际暴露更新(5 个)
    finally:
        client.close()


def test_schema_is_openai_shaped():
    client = MCPClient(MCP_URL)
    try:
        for tool in client.connect():
            assert tool["type"] == "function"
            func = tool["function"]
            assert func["name"] and func["description"]
            params = func["parameters"]
            assert params["type"] == "object" and "properties" in params
            # 身份透传保留参数不应暴露给模型(converter 已过滤)
            assert "ctx_user_id" not in params.get("properties", {})
    finally:
        client.close()


def test_mcp_tool_call_stateless():
    """调用无归属工具 query_product(不依赖当前用户,避开订单归属校验)。"""
    client = MCPClient(MCP_URL)
    try:
        client.connect()
        data = json.loads(client.call_tool("query_product", {"keyword": "耳机"}))
        assert data.get("success") is True
    finally:
        client.close()


def test_tool_manager_mcp_merges_local_extras():
    """MCP 模式:MCP 工具 + 本地独有工具合并(不硬编码总数)。"""
    manager = ToolManager(use_mcp=True, mcp_server_url=MCP_URL)
    try:
        names = {d["function"]["name"] for d in manager.tool_definitions}
        assert EXPECTED_TOOLS <= names               # MCP 5 个都在
        assert "list_user_orders" in names           # 本地独有工具仍并入
        data = json.loads(manager.execute_tool("query_product", {"keyword": "耳机"}))
        assert data.get("success") is True
    finally:
        manager.close()


def test_tool_manager_local_has_full_toolset():
    """本地模式:加载全量本地工具(断言关键工具在,不硬编码计数)。"""
    manager = ToolManager(use_mcp=False)
    try:
        names = {d["function"]["name"] for d in manager.tool_definitions}
        assert {"query_order", "query_product", "list_user_orders", "save_user_memory"} <= names
        data = json.loads(manager.execute_tool("query_product", {"keyword": "耳机"}))
        assert data.get("success") is True
    finally:
        manager.close()
