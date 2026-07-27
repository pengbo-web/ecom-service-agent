"""hmdp 电商 MCP server(FastMCP, Streamable HTTP :9123)。

把 hmdp 的 REST 包成 agent 认识的工具:返回 json 字符串,字段对齐 agent 契约,
金额分→元,议价底价剔除。每个订单/用户相关工具带 ctx_user_id(跨进程身份透传),
首行 set_current_user 落地,再按归属过滤。

impl 函数与 @mcp.tool 装饰器分离,便于单测(测试直接调 _xxx_impl)。
运行:python mcp_server/hmdp_server.py
"""

import json

from mcp.server.fastmcp import FastMCP

from mcp_server.hmdp_client import HmdpClient
from mcp_server.hmdp_mapping import map_product, map_order, map_logistics
from app.agent.runtime_context import set_current_user

_client = HmdpClient()
mcp = FastMCP("hmdp-ecom", host="127.0.0.1", port=9123)


def _dump(d) -> str:
    return json.dumps(d, ensure_ascii=False)


# ---------- 只读工具 impl ----------
def _query_product_impl(keyword: str, ctx_user_id: str = "") -> str:
    set_current_user(ctx_user_id or None)
    res = _client.get_json("/product/list", params={"keyword": keyword})
    if not res.get("success"):
        return _dump({"success": False, "error": res.get("errorMsg") or "查询失败"})
    prods = [map_product(p, public=True) for p in (res.get("data") or [])]
    return _dump({"success": True, "products": prods})


def _query_order_impl(order_id: str, ctx_user_id: str = "") -> str:
    set_current_user(ctx_user_id or None)
    res = _client.get_json(f"/order/{order_id}")
    if not res.get("success") or not res.get("data"):
        return _dump({"success": False, "error": f"未找到订单 {order_id}，请核实订单号"})
    o = map_order(res["data"])
    if ctx_user_id and o.get("user") != str(ctx_user_id):
        return _dump({"success": False, "error": f"未找到订单 {order_id}，请核实订单号"})
    return _dump({"success": True, "order": o})


def _list_user_orders_impl(ctx_user_id: str = "") -> str:
    set_current_user(ctx_user_id or None)
    if not ctx_user_id:
        return _dump({"success": False, "error": "未识别当前用户"})
    res = _client.get_json("/order/of/me", token=_token_for(ctx_user_id))
    orders = [map_order(o) for o in (res.get("data") or [])] if res.get("success") else []
    brief = [{"order_id": o["order_id"], "status": o["status"], "total": o["total"]} for o in orders]
    return _dump({"success": True, "count": len(brief), "orders": brief})


def _query_logistics_impl(order_id: str, ctx_user_id: str = "") -> str:
    set_current_user(ctx_user_id or None)
    od = _client.get_json(f"/order/{order_id}")
    if not od.get("success") or not od.get("data"):
        return _dump({"success": False, "error": "未找到订单"})
    o = map_order(od["data"])
    if ctx_user_id and o.get("user") != str(ctx_user_id):
        return _dump({"success": False, "error": "未找到订单"})
    tn = o.get("tracking_number")
    if not tn:
        return _dump({"success": False, "error": "订单尚未发货，暂无物流信息"})
    lg = _client.get_json(f"/logistics/{tn}")
    if not lg.get("success"):
        return _dump({"success": False, "error": "暂无物流信息"})
    return _dump({"success": True, "logistics": map_logistics(lg["data"])})


# userId → hmdp 登录 token。Phase 3 Task 3.2 实到(共享 Redis 反查 login:token:{token});
# 现阶段桩返回空(只读接口不需登录态;写/我的订单需登录,待 Task 3.2 打通)。
def _token_for(user_id: str) -> str:
    return ""


# ---------- MCP 工具(薄装饰,签名带 ctx_user_id) ----------
@mcp.tool()
def query_product(keyword: str, ctx_user_id: str = "") -> str:
    """根据商品名称关键词或商品ID查询商品信息，包括价格、库存、规格等。"""
    return _query_product_impl(keyword, ctx_user_id)


@mcp.tool()
def query_order(order_id: str, ctx_user_id: str = "") -> str:
    """查询指定订单的状态与明细。"""
    return _query_order_impl(order_id, ctx_user_id)


@mcp.tool()
def list_user_orders(ctx_user_id: str = "") -> str:
    """列出当前用户的全部订单概要。"""
    return _list_user_orders_impl(ctx_user_id)


@mcp.tool()
def query_logistics(order_id: str, ctx_user_id: str = "") -> str:
    """查询指定订单的物流轨迹。"""
    return _query_logistics_impl(order_id, ctx_user_id)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
