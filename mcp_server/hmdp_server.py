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


#: 拿不到当前用户时的统一说法。与 `_list_user_orders_impl` 共用同一句。
_NO_IDENTITY = "未识别当前用户"


def _query_order_impl(order_id: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    # 必须无条件先设一次:除了把身份传下去,它还负责**清掉上一次请求残留的身份**
    # ——这是个长驻进程,少设一次就可能让下一个工具继承别人的身份。
    set_current_user(ctx_user_id or None)
    # **拿不到身份 = 拒**,不是"跳过归属检查"(走查时实测出的越权口径不一致)。
    #
    # 改造前这里写的是 `if ctx_user_id and o.get("user") != ...`——`ctx_user_id` 为空时
    # 整个归属检查被跳过,于是**任何订单都能查出来**。同一个文件里的
    # `_list_user_orders_impl` 对空身份是 `return 未识别当前用户`,本地那一侧的
    # `app/agent/tools/ownership.py:owned_order` 也是拿不到身份即拒——**只有这两个读
    # 工具是反的,而它们恰好是会吐运单号和派送轨迹的那两个**(见 `_query_logistics_impl`)。
    #
    # 空 ctx_user_id 是真会发生的:`manager.py` 传的是 `get_current_user() or ""`,
    # 而 `app/api/streaming.py` 里那句 "P0-1:重放在新线程,须设身份否则 owned_order 判空"
    # 就是一次漏设身份的记录;卖家侧/协作 Agent 本来就没有买家身份。
    #
    # 上游的 token 校验(`_tok`)是另一层防线,但它是 hmdp 的门控、有自己的开关与
    # demo token,不能拿它替代本层的判定——两层都该是关着的。
    if not ctx_user_id:
        return _dump({"success": False, "error": _NO_IDENTITY})
    res = _client.get_json(f"/order/{order_id}", token=_tok(ctx_token))   # 登录保护
    if not res.get("success") or not res.get("data"):
        return _dump({"success": False, "error": f"未找到订单 {order_id}，请核实订单号"})
    o = map_order(res["data"])
    if o.get("user") != str(ctx_user_id):
        return _dump({"success": False, "error": f"未找到订单 {order_id}，请核实订单号"})
    return _dump({"success": True, "order": o})


def _list_user_orders_impl(ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    if not ctx_user_id:
        return _dump({"success": False, "error": _NO_IDENTITY})
    res = _client.get_json("/order/of/me", token=_tok(ctx_token))
    # 上游失败必须如实报错,**不能返回 success:True + 空列表**。
    #
    # 改造前正是那样写的,后果是最危险的一种:Agent 被告知"这位买家一笔订单
    # 都没有",于是自信地对买家说"您名下没有订单"——而真相是订单服务没连上。
    # 旁边的 `_query_order_impl` / `_list_products_impl` 都是先判 success 再走,
    # 只有这一个漏了。
    if not res.get("success"):
        return _dump({"success": False,
                      "error": res.get("errorMsg") or "订单服务暂时不可用，请稍后再试"})
    orders = [map_order(o) for o in (res.get("data") or [])]
    # `has_tracking` 让"这单没有物流单号"与"这份清单不带物流字段"成为两件可分辨的事。
    #
    # 实测踩过:买家问某单物流,Agent 只调了本工具(清单里没有 tracking 字段),
    # 就对买家说"系统在**多次查询**中均未匹配到对应物流单号"——它既没做过那些
    # 查询,该单在 hmdp 里也确实有单号(SF1234567890/顺丰)。清单静默丢字段,
    # 等于邀请模型把"我没查"说成"查不到"。
    brief = [{"order_id": o["order_id"], "status": o["status"],
              "status_text": o["status_text"], "total": o["total"],
              "has_tracking": bool(o.get("tracking_number"))} for o in orders]
    return _dump({"success": True, "count": len(brief), "orders": brief,
                  # 明确声明这是概要:物流轨迹、商品明细、收货地址都不在这里,
                  # 要用 query_logistics / query_order 单独取。
                  "note": "概要清单，不含物流轨迹与商品明细；"
                          "has_tracking 为 true 的订单可用 query_logistics 查轨迹"})


def _query_logistics_impl(order_id: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    # 同 `_query_order_impl`:拿不到身份即拒。这一个尤其要紧——它返回**运单号 + 完整
    # 派送轨迹(含途经位置)**,是本文件里泄漏面最大的一个。
    if not ctx_user_id:
        return _dump({"success": False, "error": _NO_IDENTITY})
    od = _client.get_json(f"/order/{order_id}", token=_tok(ctx_token))
    if not od.get("success") or not od.get("data"):
        return _dump({"success": False, "error": "未找到订单"})
    o = map_order(od["data"])
    if o.get("user") != str(ctx_user_id):
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
    # 无身份的**写**操作根本不该发出去:上游 token 校验会拒,但那时错误已经
    # 变成一句含糊的"操作失败"。与 `_place_order_impl` 显式挡空 token 同一条取舍。
    if not ctx_user_id:
        return _dump({"success": False, "message": _NO_IDENTITY})
    res = _client.post_json(f"/order/{order_id}/refund", {"reason": reason}, token=_tok(ctx_token))
    return _dump(_hmdp_write(res))


def _cancel_order_impl(order_id: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    # 无身份的**写**操作根本不该发出去:上游 token 校验会拒,但那时错误已经
    # 变成一句含糊的"操作失败"。与 `_place_order_impl` 显式挡空 token 同一条取舍。
    if not ctx_user_id:
        return _dump({"success": False, "message": _NO_IDENTITY})
    res = _client.post_json(f"/order/{order_id}/cancel", {}, token=_tok(ctx_token))
    return _dump(_hmdp_write(res))


def _change_address_impl(order_id: str, new_address: str, ctx_user_id: str = "", ctx_token: str = "") -> str:
    set_current_user(ctx_user_id or None)
    # 无身份的**写**操作根本不该发出去:上游 token 校验会拒,但那时错误已经
    # 变成一句含糊的"操作失败"。与 `_place_order_impl` 显式挡空 token 同一条取舍。
    if not ctx_user_id:
        return _dump({"success": False, "message": _NO_IDENTITY})
    res = _client.put_json(f"/order/{order_id}/address", {"address": new_address}, token=_tok(ctx_token))
    return _dump(_hmdp_write(res))


def _place_order_impl(product_id: str, quantity=1, address: str = "",
                      ctx_user_id: str = "", ctx_token: str = "") -> str:
    """创建一张【待支付】订单(不代付款)。hmdp POST /order 建 status=pending 订单,无资金流动。"""
    set_current_user(ctx_user_id or None)
    if not ctx_token:
        return _dump({"success": False, "message": "请先登录后再下单哦～"})
    try:
        qty = int(quantity)
    except (TypeError, ValueError):
        qty = 1
    pid = int(product_id) if str(product_id).isdigit() else product_id
    res = _client.post_json("/order", {"productId": pid, "quantity": qty, "address": address},
                            token=_tok(ctx_token))
    if res.get("success"):
        no = res.get("data")
        return _dump({"success": True, "order_no": no,
                      "message": (f"已为您创建订单 {no}(状态:待支付)。"
                                  "请到「我的订单」完成支付——支付这一步需您本人操作,小夕不会代付。")})
    return _dump({"success": False, "message": res.get("errorMsg") or "下单失败,请稍后再试"})


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
def place_order(product_id: str, quantity: int = 1, address: str = "",
                ctx_user_id: str = "", ctx_token: str = "") -> str:
    """为用户创建一张【待支付】订单(不代付款)。需商品ID、购买数量、收货地址;
    下单前应先与用户确认商品/数量/收货地址。创建后订单为待支付状态,支付由用户本人完成。"""
    return _place_order_impl(product_id, quantity, address, ctx_user_id, ctx_token)


@mcp.tool()
def negotiate_price(product_id: str, buyer_offer: Optional[float] = None,
                    ctx_user_id: str = "", ctx_token: str = "") -> str:
    """针对指定商品进行一轮议价。buyer_offer 为买家出价（元），未报价可省略。"""
    return _negotiate_price_impl(product_id, buyer_offer, ctx_user_id, ctx_token)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
