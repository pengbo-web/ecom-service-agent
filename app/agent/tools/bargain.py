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
