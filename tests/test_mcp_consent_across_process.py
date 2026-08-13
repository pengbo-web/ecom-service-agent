"""确认门要能跨到 MCP server 进程,而且不能被模型自授权。

**实测缺陷**(走查 MCP 全链路时抓到,真起了 server 跑出来的)。

`app/agent/consent.py` 的确认门用 ContextVar 实现,而 MCP 工具在**独立进程**里执行。
`ToolManager.execute_tool` 的注释已经点明了这个问题并处理了身份:

    # 跨进程身份透传:MCP 工具在独立 server 进程执行,ContextVar 传不过去,
    # 用保留参数把当前用户带过去
    args = {**arguments, "ctx_user_id": ..., "ctx_token": ...}

**但漏了确认门。** 实测(mcp_enabled=True,MCP server 真起着):

    客户端已 consent_scope(['refund'])
    MCP 路径 apply_refund → {"success": false, "need_confirm": true,
                             "message": "退款是敏感操作。请确认是否…办理退款？"}
    订单状态:shipped(没变)

买家确认 → 工具再问一次确认 → 客服再问 → **退款在 MCP 路径上永远完不成**。
方向是安全的(不会越权执行),但功能是坏的,而 `mcp_enabled=True` 就是当前部署配置。

MCP 暴露的 5 个工具里只有 `apply_refund` 受确认门管(`query_order`/`query_product`/
`query_logistics`/`search_knowledge` 都是只读),所以影响面就是退款这一条——但退款
恰好是最不能"坏在安静处"的那一条。

**归属校验这一半是好的**:实测用小明的订单 + ctx_user_id='小明' 能过归属、走到确认门,
`ctx_user_id` 那套透传是有效的。

修法与 `ctx_user_id` 同一套:客户端读 `allowed_actions()` 用保留参数 `ctx_consent`
带过去,server 端 `consent_scope(...)` 落地。保留参数放在 `**arguments` **之后**,
所以模型自己在 arguments 里塞 `ctx_consent` 会被真值覆盖——**自授权无效**(有测试)。
"""

import json

import pytest

from app.agent.consent import allowed_actions, consent_scope


# --------------------------------------------------------------------------
# 读取口
# --------------------------------------------------------------------------

def test_allowed_actions_reflects_scope():
    assert allowed_actions() == frozenset()
    with consent_scope(["refund"]):
        assert allowed_actions() == frozenset({"refund"})
    assert allowed_actions() == frozenset(), "退出作用域没复位"


def test_allowed_actions_handles_none():
    with consent_scope(None):
        assert allowed_actions() == frozenset()


# --------------------------------------------------------------------------
# 客户端:保留参数必须带上,且必须压过模型给的同名参数
# --------------------------------------------------------------------------

class _RecordingMCP:
    """记下客户端最终发出去的参数。"""

    def __init__(self):
        self.calls = []

    def call_tool(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"success": True})


def _manager_with(mcp):
    from app.agent.tools.manager import ToolManager

    tm = ToolManager.__new__(ToolManager)          # 不走 __init__,避免真连 MCP
    tm._mcp_client = mcp
    tm._tool_source = {"apply_refund": "mcp"}
    tm._shared_mcp = True
    return tm


def test_consent_is_sent_across_the_boundary():
    """**核心断言。** 修复前这个键根本不存在,server 端只能看到空授权。"""
    mcp = _RecordingMCP()
    tm = _manager_with(mcp)
    with consent_scope(["refund"]):
        tm.execute_tool("apply_refund", {"order_id": "O1", "reason": "r"})

    _, args = mcp.calls[0]
    assert args["ctx_consent"] == "refund"


def test_no_consent_sends_empty_string():
    """没授权时传空串,server 端解析成空集合——与改造前"没有任何授权"一致。"""
    mcp = _RecordingMCP()
    tm = _manager_with(mcp)
    tm.execute_tool("apply_refund", {"order_id": "O1", "reason": "r"})
    assert mcp.calls[0][1]["ctx_consent"] == ""


def test_model_cannot_self_authorize():
    """**模型自己在 arguments 里塞 ctx_consent 必须无效。**

    保留参数放在 `**arguments` 之后,真值覆盖模型给的值。这条同时守住
    ctx_user_id——否则模型能自称是任何人。
    """
    mcp = _RecordingMCP()
    tm = _manager_with(mcp)
    tm.execute_tool("apply_refund",
                    {"order_id": "O1", "reason": "r",
                     "ctx_consent": "refund", "ctx_user_id": "victim"})

    _, args = mcp.calls[0]
    assert args["ctx_consent"] == "", "模型自授权成功了"
    assert args["ctx_user_id"] == "", "模型冒充了别的用户"


def test_multiple_actions_are_stable_and_sorted():
    """排序是为了让这个字段可比对(日志/断言),不是功能需要。"""
    mcp = _RecordingMCP()
    tm = _manager_with(mcp)
    with consent_scope(["refund", "cancel_order"]):
        tm.execute_tool("apply_refund", {"order_id": "O1", "reason": "r"})
    assert mcp.calls[0][1]["ctx_consent"] == "cancel_order,refund"


# --------------------------------------------------------------------------
# server 端:落地与解析
# --------------------------------------------------------------------------

def test_server_side_consent_parsing():
    from mcp_server.server import _consent_from

    with _consent_from("refund"):
        assert allowed_actions() == frozenset({"refund"})
    assert allowed_actions() == frozenset(), "作用域没退出"


def test_server_side_empty_means_no_authorization():
    """空串/None 都要落成空集合,绝不能理解成"全放行"。"""
    from mcp_server.server import _consent_from

    for value in ("", None, ",,"):
        with _consent_from(value):
            assert allowed_actions() == frozenset(), f"{value!r} 被当成了授权"


def test_server_side_multiple_actions():
    from mcp_server.server import _consent_from

    with _consent_from("cancel_order,refund"):
        assert allowed_actions() == frozenset({"cancel_order", "refund"})


# --------------------------------------------------------------------------
# 端到端(需要真的 MCP server 在跑,没跑就跳过)
# --------------------------------------------------------------------------

@pytest.fixture()
def live_mcp():
    import socket

    from app.config.settings import settings

    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 9123))
    except OSError:
        pytest.skip("MCP server 未运行(python mcp_server/server.py)")
    finally:
        s.close()

    from app.agent.tools.manager import ToolManager
    return ToolManager(use_mcp=True, mcp_server_url=settings.mcp_server_url)


@pytest.fixture()
def mcp_order():
    from app.db import get_db

    conn = get_db().connect()
    conn.execute("DELETE FROM orders WHERE order_id='MCP-CONSENT-TEST'")
    conn.execute("INSERT INTO orders (order_id,user,status,total) VALUES (?,?,?,?)",
                 ("MCP-CONSENT-TEST", "mcpuser", "shipped", 100.0))
    conn.commit()
    conn.close()
    yield "MCP-CONSENT-TEST"
    conn = get_db().connect()
    conn.execute("DELETE FROM orders WHERE order_id='MCP-CONSENT-TEST'")
    conn.commit()
    conn.close()


def test_end_to_end_consent_grants_and_blocks(live_mcp, mcp_order):
    """真跨进程跑一遍:没授权挡住、授权了执行。两个方向都要断言。

    只断言"授权了能过"会漏掉最危险的情形——把门拆了同样能让这条通过。
    """
    from app.agent.runtime_context import set_current_user
    from app.db import get_db

    set_current_user("mcpuser")

    blocked = json.loads(live_mcp.execute_tool(
        "apply_refund", {"order_id": mcp_order, "reason": "test"}))
    assert blocked["need_confirm"] is True
    assert get_db().get_order(mcp_order)["status"] == "shipped"

    with consent_scope(["refund"]):
        done = json.loads(live_mcp.execute_tool(
            "apply_refund", {"order_id": mcp_order, "reason": "尺码不合适"}))
    assert done["success"] is True
    assert get_db().get_order(mcp_order)["status"] == "refund_processing"
