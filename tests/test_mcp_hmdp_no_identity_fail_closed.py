"""hmdp-MCP:拿不到身份时必须拒,不能"跳过归属检查"。

**实测缺陷**(走查 MCP 路径时抓到)。同一个文件里三个读工具对"空 `ctx_user_id`"的处理
方向相反:

    _list_user_orders_impl:  if not ctx_user_id: return 未识别当前用户        ← 拒
    _query_order_impl:       if ctx_user_id and o["user"] != ctx_user_id: ...  ← 跳过检查
    _query_logistics_impl:   if ctx_user_id and o["user"] != ctx_user_id: ...  ← 跳过检查

**而 fail-open 的偏偏是会吐 PII 的那两个。** 直接调 impl 实测(stub 掉 HmdpClient):

    query_order      (ctx_user_id="") → success:true,返回完整订单(含属主 user="1")
    query_logistics  (ctx_user_id="") → success:true,返回 SF1234567890 + 完整派送轨迹(含途经位置)
    list_user_orders (ctx_user_id="") → success:false,未识别当前用户

本地那一侧的 `app/agent/tools/ownership.py:owned_order` 也是**拿不到身份即拒**
(它的 docstring 写着"拿不到当前用户 → 拒(fail-closed,订单是隐私)")。
所以这两个工具是整套里唯一反着的。

**空 `ctx_user_id` 是真会发生的**:`app/agent/tools/manager.py` 传的是
`get_current_user() or ""`;`app/api/streaming.py` 里那句注释
"P0-1:重放在新线程,须设身份否则 owned_order 判空"就是一次漏设身份的现场记录;
卖家侧与协作 Agent 本来就没有买家身份。

上游 token 校验(`_tok`)是另一层防线,但它是 hmdp 的门控、有自己的开关与 demo token,
不能拿它替代本层判定——**两层都该是关着的**。

既有测试 `tests/test_hmdp_server_tools.py` 每一条都传了非空 `ctx_user_id`,
所以这个分支一次都没被覆盖过。
"""

import json
from unittest.mock import patch

import pytest

import mcp_server.hmdp_server as srv


ORDER = {"success": True, "data": {"order_no": "ORD-1", "user_id": 1, "status": "shipped",
         "total": 89900, "tracking_number": "SF1234567890", "items": []}}
LOGISTICS = {"success": True, "data": {"tracking_number": "SF1234567890", "carrier": "顺丰",
             "status": "in_transit",
             "events": [{"time": "t", "location": "上海浦东", "description": "派送中"}]}}


def _fake_get(path, params=None, token=None):
    return LOGISTICS if path.startswith("/logistics/") else ORDER


# --------------------------------------------------------------------------
# 核心:空身份 → 拒
# --------------------------------------------------------------------------

def test_query_order_refuses_without_identity():
    """修复前:success:true + 完整订单。"""
    with patch.object(srv._client, "get_json", side_effect=_fake_get):
        out = json.loads(srv._query_order_impl("ORD-1", ctx_user_id=""))
    assert out["success"] is False, f"空身份仍然查出了订单: {out}"
    assert out["error"] == srv._NO_IDENTITY


def test_query_logistics_refuses_without_identity():
    """**泄漏面最大的那个**:修复前会返回运单号 + 完整派送轨迹(含途经位置)。"""
    with patch.object(srv._client, "get_json", side_effect=_fake_get):
        out = json.loads(srv._query_logistics_impl("ORD-1", ctx_user_id=""))
    assert out["success"] is False, f"空身份仍然拿到了物流: {out}"
    assert "SF1234567890" not in json.dumps(out, ensure_ascii=False)


def test_no_upstream_request_is_made_without_identity():
    """连请求都不该发出去:少一次带不上身份的上游调用,也少一条误导性的上游日志。"""
    with patch.object(srv._client, "get_json", side_effect=_fake_get) as m:
        srv._query_order_impl("ORD-1", ctx_user_id="")
        srv._query_logistics_impl("ORD-1", ctx_user_id="")
    assert m.call_count == 0


def test_list_user_orders_behaviour_unchanged():
    """它本来就是对的,这次改动不能把它弄反。"""
    with patch.object(srv._client, "get_json", side_effect=_fake_get):
        out = json.loads(srv._list_user_orders_impl(ctx_user_id=""))
    assert out["success"] is False and out["error"] == srv._NO_IDENTITY


# --------------------------------------------------------------------------
# 反向:有身份时一切照旧(修复不能把正常路径掐掉)
# --------------------------------------------------------------------------

def test_owner_still_reads_own_order():
    with patch.object(srv._client, "get_json", side_effect=_fake_get):
        out = json.loads(srv._query_order_impl("ORD-1", ctx_user_id="1"))
    assert out["success"] is True and out["order"]["status"] == "shipped"


def test_owner_still_reads_own_logistics():
    with patch.object(srv._client, "get_json", side_effect=_fake_get):
        out = json.loads(srv._query_logistics_impl("ORD-1", ctx_user_id="1"))
    assert out["success"] is True
    assert out["logistics"]["tracking_number"] == "SF1234567890"


def test_other_user_still_refused():
    with patch.object(srv._client, "get_json", side_effect=_fake_get):
        o = json.loads(srv._query_order_impl("ORD-1", ctx_user_id="999"))
        l = json.loads(srv._query_logistics_impl("ORD-1", ctx_user_id="999"))
    assert o["success"] is False and l["success"] is False


# --------------------------------------------------------------------------
# 身份必须被无条件重设:长驻进程里少设一次 = 继承别人的身份
# --------------------------------------------------------------------------

def test_identity_is_reset_even_when_refusing():
    """**这条锁住我改这个文件时当场犯的错。**

    第一版替换 `_query_order_impl` 时把开头的 `set_current_user` 一起删掉了。
    它除了把身份传下去,还负责**清掉上一次请求残留的身份**——这是个长驻进程,
    少设一次就可能让后续代码继承上一个买家的身份。
    """
    from app.agent.runtime_context import get_current_user, set_current_user

    set_current_user("上一个买家")
    with patch.object(srv._client, "get_json", side_effect=_fake_get):
        srv._query_order_impl("ORD-1", ctx_user_id="")     # 空身份,会被拒
    assert get_current_user() in (None, ""), "残留了上一个请求的身份"


@pytest.mark.parametrize("fn,args", [
    ("_query_order_impl", ("ORD-1",)),
    ("_query_logistics_impl", ("ORD-1",)),
    ("_apply_refund_impl", ("ORD-1", "不想要了")),
    ("_cancel_order_impl", ("ORD-1",)),
    ("_change_address_impl", ("ORD-1", "新地址")),
    ("_list_user_orders_impl", ()),
])
def test_every_order_tool_resets_identity(fn, args):
    """每个碰订单的 impl 都要无条件重设身份,一个都不能漏。"""
    from app.agent.runtime_context import get_current_user, set_current_user

    set_current_user("上一个买家")
    with patch.object(srv._client, "get_json", side_effect=_fake_get), \
         patch.object(srv._client, "post_json", return_value={"success": True, "data": "ok"}), \
         patch.object(srv._client, "put_json", return_value={"success": True, "data": "ok"}):
        getattr(srv, fn)(*args, ctx_user_id="")
    assert get_current_user() in (None, ""), f"{fn} 没有重设身份"


# --------------------------------------------------------------------------
# 写工具:无身份的写请求根本不该发出去
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fn,args", [
    ("_apply_refund_impl", ("ORD-1", "不想要了")),
    ("_cancel_order_impl", ("ORD-1",)),
    ("_change_address_impl", ("ORD-1", "新地址")),
])
def test_writes_refuse_without_identity(fn, args):
    """上游 token 校验会拒,但那时错误已经变成一句含糊的"操作失败"。
    与 `_place_order_impl` 显式挡空 token 是同一条取舍。"""
    with patch.object(srv._client, "post_json") as post, \
         patch.object(srv._client, "put_json") as put:
        out = json.loads(getattr(srv, fn)(*args, ctx_user_id=""))
    assert out["success"] is False and out["message"] == srv._NO_IDENTITY
    assert post.call_count == 0 and put.call_count == 0, "无身份的写请求被发到了上游"


def test_writes_still_work_with_identity():
    with patch.object(srv._client, "post_json", return_value={"success": True, "data": "已受理"}):
        out = json.loads(srv._apply_refund_impl("ORD-1", "不想要了", ctx_user_id="1"))
    assert out["success"] is True


# --------------------------------------------------------------------------
# 本地 DB 那个 MCP server 不受影响(它委托给 owned_order)
# --------------------------------------------------------------------------

def test_local_mcp_server_delegates_to_owned_order():
    """`mcp_server/server.py` 的读工具走本地工具函数,而那些用 `owned_order`
    (fail-closed)。这条锁住"洞只在 hmdp_server"这个判断,别下次又去改错文件。"""
    import inspect

    import mcp_server.server as local

    src = inspect.getsource(local)
    assert "set_current_user(ctx_user_id or None)" in src
    # 它自己不做归属比较,归属由被委托的本地工具(owned_order)负责
    assert 'o.get("user")' not in src
