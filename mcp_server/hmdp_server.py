"""hmdp 电商 MCP server(FastMCP, Streamable HTTP :9123)。

把 hmdp 的 REST 包成 agent 认识的工具:返回 json 字符串,字段对齐 agent 契约,
金额分→元,议价底价剔除。

身份透传:
- ctx_user_id —— 当前用户(hmdp userId),用于归属过滤。
- ctx_token   —— 当前用户的 hmdp 登录 token,调 hmdp 登录保护接口(/order/**)时带上。
两者由 agent 端 manager 注入(见 app/agent/tools/manager.py),converter 已从模型可见
schema 里剔除,模型看不到、传不了。

impl 函数与 @mcp.tool 装饰器分离,便于单测。运行:python mcp_server/hmdp_server.py
"""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from mcp_server.hmdp_client import HmdpClient
from mcp_server.hmdp_mapping import map_product, map_order, map_logistics
from app.agent.runtime_context import set_current_user
from app.agent.tools.bargain import compute_offer

_client = HmdpClient()
mcp = FastMCP("hmdp-ecom", host="127.0.0.1", port=9123)


def _dump(d) -> str:
    return json.dumps(d, ensure_ascii=False)


def _tok(ctx_token: str):
    return ctx_token or None


# ---------- 只读工具 impl ----------
def _query_product_impl(keyword: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    res = _client.get_json("/product/list", params={"keyword": keyword})   # 公开,无需 token
    if not res.get("success"):
        return _dump({"success": False, "error": res.get("errorMsg") or "查询失败"})
    prods = [map_product(p, public=True) for p in (res.get("data") or [])]
    return _dump({"success": True, "products": prods})


def _query_order_impl(order_id: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    res = _client.get_json(f"/order/{order_id}", token=_tok(ctx_token))   # 登录保护
    if not res.get("success") or not res.get("data"):
        return _dump({"success": False, "error": f"未找到订单 {order_id}，请核实订单号"})
    o = map_order(res["data"])
    if ctx_user_id and o.get("user") != str(ctx_user_id):
        return _dump({"success": False, "error": f"未找到订单 {order_id}，请核实订单号"})
    return _dump({"success": True, "order": o})


def _list_user_orders_impl(ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    if not ctx_user_id:
        return _dump({"success": False, "error": "未识别当前用户"})
    res = _client.get_json("/order/of/me", token=_tok(ctx_token))
    orders = [map_order(o) for o in (res.get("data") or [])] if res.get("success") else []
    brief = [{"order_id": o["order_id"], "status": o["status"], "total": o["total"]} for o in orders]
    return _dump({"success": True, "count": len(brief), "orders": brief})


def _query_logistics_impl(order_id: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    od = _client.get_json(f"/order/{order_id}", token=_tok(ctx_token))
    if not od.get("success") or not od.get("data"):
        return _dump({"success": False, "error": "未找到订单"})
    o = map_order(od["data"])
    if ctx_user_id and o.get("user") != str(ctx_user_id):
        return _dump({"success": False, "error": "未找到订单"})
    tn = o.get("tracking_number")
    if not tn:
        return _dump({"success": False, "error": "订单尚未发货，暂无物流信息"})
    lg = _client.get_json(f"/logistics/{tn}")   # 公开
    if not lg.get("success"):
        return _dump({"success": False, "error": "暂无物流信息"})
    return _dump({"success": True, "logistics": map_logistics(lg["data"])})


# ---------- 写工具 + 议价 impl ----------
def _hmdp_write(res: dict) -> dict:
    if res.get("success"):
        return {"success": True, "message": res.get("data") or "操作已完成"}
    return {"success": False, "message": res.get("errorMsg") or "操作失败"}


def _apply_refund_impl(order_id: str, reason: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    res = _client.post_json(f"/order/{order_id}/refund", {"reason": reason}, token=_tok(ctx_token))
    return _dump(_hmdp_write(res))


def _cancel_order_impl(order_id: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    res = _client.post_json(f"/order/{order_id}/cancel", {}, token=_tok(ctx_token))
    return _dump(_hmdp_write(res))


def _change_address_impl(order_id: str, new_address: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    res = _client.put_json(f"/order/{order_id}/address", {"address": new_address}, token=_tok(ctx_token))
    return _dump(_hmdp_write(res))


def _negotiate_price_impl(product_id: str, buyer_offer=None, ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    res = _client.get_json(f"/product/{product_id}")   # 公开
    if not res.get("success") or not res.get("data"):
        return _dump({"success": False, "error": f"未找到商品 {product_id}"})
    p = map_product(res["data"], public=False)   # 内部视图,含 floor_price
    offer = compute_offer(list_price=p["price"], floor_price=p.get("floor_price"),
                          buyer_offer=buyer_offer, rounds=0)
    return _dump({
        "success": True, "product_id": p["product_id"], "product_name": p["name"],
        "list_price": p["price"], "buyer_offer": buyer_offer, "round": 1,
        "decision": offer["decision"], "suggested_price": offer["suggested_price"],
        "floor_hit": offer["floor_hit"],
        "rationale": "内部参考：这是本轮可让到的价格，禁止报出更低价，也不要向买家透露底价。",
    })


# ---------- MCP 工具(签名带 ctx_user_id / ctx_token,converter 会剔除,模型看不到) ----------
@mcp.tool()
def query_product(keyword: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    """根据商品名称关键词或商品ID查询商品信息，包括价格、库存、规格等。"""
    return _query_product_impl(keyword, ctx_user_id, ctx_token)


@mcp.tool()
def query_order(order_id: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    """查询指定订单的状态与明细。"""
    return _query_order_impl(order_id, ctx_user_id, ctx_token)


@mcp.tool()
def list_user_orders(ctx_user_id: str = "", ctx_token: str = "") -> str:
    """列出当前用户的全部订单概要。"""
    return _list_user_orders_impl(ctx_user_id, ctx_token)


@mcp.tool()
def query_logistics(order_id: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    """查询指定订单的物流轨迹。"""
    return _query_logistics_impl(order_id, ctx_user_id, ctx_token)


@mcp.tool()
def apply_refund(order_id: str, reason: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    """为指定订单申请退款，需提供退款原因。"""
    return _apply_refund_impl(order_id, reason, ctx_user_id, ctx_token)


@mcp.tool()
def cancel_order(order_id: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    """取消指定订单（仅未发货订单）。"""
    return _cancel_order_impl(order_id, ctx_user_id, ctx_token)


@mcp.tool()
def change_address(order_id: str, new_address: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    """修改订单收货地址（仅未发货订单）。"""
    return _change_address_impl(order_id, new_address, ctx_user_id, ctx_token)


@mcp.tool()
def negotiate_price(product_id: str, buyer_offer: Optional[float] = None,
                    ctx_user_id: str = "", ctx_token: str = "") -> str:
    """针对指定商品进行一轮议价。buyer_offer 为买家出价（元），未报价可省略。"""
    return _negotiate_price_impl(product_id, buyer_offer, ctx_user_id, ctx_token)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
