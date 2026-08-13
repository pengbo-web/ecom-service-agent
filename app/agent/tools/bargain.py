"""议价工具：确定性阶梯让价 + negotiate_price 工具入口。

LLM 负责话术，工具负责算钱。让价金额由纯函数 compute_offer 计算，可单测、可复现。
"""

from __future__ import annotations

import contextvars
from typing import Optional

from app.config.settings import settings
from app.db import get_db

import logging

logger = logging.getLogger(__name__)

# 当前会话 id：由 EcomAgent.chat() 在入口绑定（worker 线程内设/读，天然隔离）
_current_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "bargain_session_id", default=None
)


def set_current_session(session_id: Optional[str]) -> None:
    """由 EcomAgent.chat() 调用，绑定当前会话 id 供 negotiate_price 读取。"""
    _current_session_id.set(session_id)


def get_current_session() -> Optional[str]:
    """当前会话 id(供幂等键等跨切面读取)。"""
    return _current_session_id.get()


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
    deal_recorded = False
    if result["decision"] == "accept":
        from app.agent.consent import is_allowed, need_confirm_result
        if not is_allowed("deal_close"):
            return need_confirm_result(
                "deal_close",
                f"可以按 ¥{result['suggested_price']} 成交 {product['name']}。"
                "请确认是否以此价下单？",
            )
        # **成交要落库,否则这个价格哪儿也去不了。**
        #
        # 改造前 accept 拿到确认之后只 `bump_bargain_state`(存的是谈判过程的
        # "本轮建议价"),没有任何"这个买家可以按这个价买"的记录;而 `POST /api/order`
        # 对议价的引用次数是 **0**,一律按标价结算。买家谈到 ¥750(标价 ¥899)下单
        # 被收 ¥899——客服刚亲口答应过的价格。
        #
        # 按 user 记而不是 session:兑现发生在下单请求里,那里没有 session_id。
        from app.agent.runtime_context import get_current_user

        buyer = get_current_user()
        if buyer:
            db.record_bargain_deal(buyer, product_id, result["suggested_price"],
                                   ttl_hours=settings.bargain_deal_ttl_hours)
            deal_recorded = True
        else:
            # 拿不到买家身份就落不了库。**如实反映在 price_effect 里**,不能让模型
            # 以为已经锁价了——那正是这条缺陷最初的形态。
            logger.warning("议价成交无法落库:拿不到当前买家身份(product_id=%s)", product_id)

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
        # **给模型的硬约束,不是提示。** 实测:第一轮议价后客服自己说了"该价格为平台
        # 授权的最优让利，**下单时将自动生效**"——而这个价格只被写进 bargain_sessions,
        # 下单路径 `POST /api/order` 对议价的引用次数是 0,它按商品标价结算。买家谈到
        # ¥750(标价 ¥899)后直接下单,会被按 ¥899 收钱。
        #
        # 这是比"平台承担运费"更硬的金钱承诺:那句还只是费用归属,这句是买家实际付多少。
        # 光靠 prompt 规则压不住(这个项目自己验证过 prompt 保得住动作、保不住话术),
        # 所以把事实放进**工具返回值**——模型看得到它,就不需要自己脑补生效方式。
        #
        # 三种取值对应三种真实状态,**不能合并**:成交并已锁价 / 还在还价 / 想锁但
        # 落库失败。第三种如果说成第一种,就退回了这条缺陷最初的形态(客服说了
        # "下单自动生效",而系统按标价收钱)。
        "price_effect": _price_effect(result["decision"], deal_recorded),
    }


def _price_effect(decision: str, deal_recorded: bool) -> str:
    """告诉模型这个价格到底会不会作用到订单——按真实状态给,不给套话。"""
    if decision != "accept":
        return ("这是本轮的还价,**尚未成交**,不会作用于订单。"
                "禁止对买家说“已锁定/已锁价/下单自动生效”。")
    if deal_recorded:
        return (f"已按此价为该买家锁定,**下单时自动生效**"
                f"(有效期 {settings.bargain_deal_ttl_hours} 小时,仅限一笔订单,"
                "过期或用掉后恢复标价)。可以如实告诉买家。")
    return ("成交价**未能锁定**(系统未取到买家身份),下单仍按标价结算。"
            "禁止对买家说“已锁定/下单自动生效”,请引导买家先登录后再议价。")
