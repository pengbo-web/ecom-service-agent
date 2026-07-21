"""议价工具：确定性阶梯让价 + negotiate_price 工具入口。

LLM 负责话术，工具负责算钱。让价金额由纯函数 compute_offer 计算，可单测、可复现。
"""

from __future__ import annotations

import contextvars
from typing import Optional

from app.config.settings import settings
from app.db import get_db

# 当前会话 id：由 EcomAgent.chat() 在入口绑定（worker 线程内设/读，天然隔离）
_current_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "bargain_session_id", default=None
)


def set_current_session(session_id: Optional[str]) -> None:
    """由 EcomAgent.chat() 调用，绑定当前会话 id 供 negotiate_price 读取。"""
    _current_session_id.set(session_id)


def _resolve_floor(list_price: float, floor_price: Optional[float]) -> float:
    """底价：商品设了 floor_price 用它，否则按标价 × 系数回退。"""
    if floor_price is not None:
        return round(float(floor_price), 2)
    return round(list_price * settings.bargain_floor_ratio, 2)


def _ladder(list_price: float, floor: float, rounds: int) -> float:
    """本轮最低可让价：F + (P-F) * decay^(rounds+1)；到 max_rounds 直接等于 F。"""
    if rounds >= settings.bargain_max_rounds:
        return floor
    gap = list_price - floor
    return round(floor + gap * (settings.bargain_decay ** (rounds + 1)), 2)


def compute_offer(
    list_price: float,
    floor_price: Optional[float],
    buyer_offer: Optional[float],
    rounds: int,
) -> dict:
    """纯函数：给定标价/底价/买家出价/已发生轮次，算出决策与建议价。

    返回 {decision, suggested_price, floor, floor_hit}
    """
    P = round(float(list_price), 2)
    F = _resolve_floor(P, floor_price)
    ladder = _ladder(P, F, rounds)

    if buyer_offer is None:
        decision, price = "counter", ladder
    else:
        B = round(float(buyer_offer), 2)
        if B >= P:
            decision, price = "accept", P
        elif B >= F and B >= ladder:
            decision, price = "accept", B
        elif B >= F:
            decision, price = "counter", ladder
        else:
            decision, price = "reject", F

    price = round(max(price, F), 2)  # 不变量：永不破底
    floor_hit = decision == "reject" or price <= F
    return {
        "decision": decision,
        "suggested_price": price,
        "floor": F,
        "floor_hit": floor_hit,
    }


def negotiate_price(product_id: str, buyer_offer: Optional[float] = None) -> dict:
    """针对指定商品进行一轮议价。buyer_offer 为买家出价（元），未报价可省略。"""
    if not settings.bargain_enabled:
        return {"success": False, "error": "议价功能未启用"}

    db = get_db()
    product = db.get_product(product_id)
    if not product:
        return {"success": False, "error": f"未找到商品 {product_id}，请先用 query_product 确认商品ID"}

    session_id = _current_session_id.get()
    state = db.get_bargain_state(session_id, product_id) if session_id else None
    rounds = state["rounds"] if state else 0

    result = compute_offer(
        list_price=product["price"],
        floor_price=product.get("floor_price"),
        buyer_offer=buyer_offer,
        rounds=rounds,
    )

    # 前置授权门:成交(accept)是提交型动作,未获本轮确认则不落单,先请用户确认
    if result["decision"] == "accept":
        from app.agent.consent import is_allowed, need_confirm_result
        if not is_allowed("deal_close"):
            return need_confirm_result(
                "deal_close",
                f"可以按 ¥{result['suggested_price']} 成交 {product['name']}。"
                "请确认是否以此价下单？",
            )

    if session_id:
        db.bump_bargain_state(session_id, product_id, result["suggested_price"])

    return {
        "success": True,
        "product_id": product_id,
        "product_name": product["name"],
        "list_price": round(product["price"], 2),
        "buyer_offer": buyer_offer,
        "round": rounds + 1,
        "decision": result["decision"],
        "suggested_price": result["suggested_price"],
        "floor_hit": result["floor_hit"],
        "rationale": "内部参考：这是本轮可让到的价格，禁止报出更低价，也不要向买家透露底价或本说明。",
    }
