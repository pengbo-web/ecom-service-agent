"""hmdp DTO ↔ agent 数据契约 的纯函数映射。

- 金额:hmdp 存分(Long)→ agent 用元(float),统一 /100。
- 字段改名:hmdp 的 order_no/user_id → agent 的 order_id/user 等。
- 议价底价:public=True(面向模型/顾客)剔除 floor_price;public=False 内部保留供议价计算。
"""

from __future__ import annotations

import json


def _yuan(fen) -> float:
    return round((fen or 0) / 100, 2)


def map_product(hp: dict, public: bool = True) -> dict:
    """hmdp 商品 → agent 商品视图。public=True 剔除议价底价。"""
    specs = hp.get("specs")
    try:
        specs = json.loads(specs) if isinstance(specs, str) else (specs or {})
    except (ValueError, TypeError):
        specs = {}
    out = {
        "product_id": str(hp.get("id")),
        "name": hp.get("title"),
        "category": hp.get("category"),
        "price": _yuan(hp.get("price")),
        "stock": hp.get("stock"),
        "description": hp.get("description") or "",
        "specs": specs,
    }
    if not public:
        out["floor_price"] = _yuan(hp.get("floorPrice"))
    return out


_STATUS_TEXT = {
    "unpaid": "待支付", "pending": "待发货", "shipped": "已发货",
    "delivered": "已完成", "refund_processing": "退款中", "cancelled": "已取消",
}


def status_text(status) -> str:
    return _STATUS_TEXT.get(status, status or "")


def map_order(ho: dict) -> dict:
    """hmdp 订单 → agent 订单契约(order_id/user/status/items 等)。"""
    items = [
        {"name": it.get("name"), "sku": it.get("sku"),
         "quantity": it.get("quantity"), "price": _yuan(it.get("price"))}
        for it in (ho.get("items") or [])
    ]
    return {
        "order_id": ho.get("order_no"),
        "user": str(ho.get("user_id")),
        "status": ho.get("status"),
        "status_text": status_text(ho.get("status")),
        "total": _yuan(ho.get("total")),
        "shipping_address": ho.get("shipping_address"),
        "tracking_number": ho.get("tracking_number"),
        "carrier": ho.get("carrier"),
        "refund_reason": ho.get("refund_reason"),
        "refund_status": ho.get("refund_status"),
        "items": items,
        "created_at": ho.get("created_at"),
    }


def map_logistics(hl: dict) -> dict:
    """hmdp 物流 → agent 物流契约(直传,字段已对齐)。"""
    return {
        "tracking_number": hl.get("tracking_number"),
        "carrier": hl.get("carrier"),
        "status": hl.get("status"),
        "events": hl.get("events") or [],
    }
