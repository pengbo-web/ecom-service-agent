"""订单归属校验:防跨用户越权(拿别人订单号查/改/退)。

所有按 order_id 操作的工具经此取单——越权视同订单不存在(返回 None),
调用方已有的"未找到订单"话术复用,不泄露订单存在性。

门控与隐私策略:
- auth_enabled=False:教学单机,放行(不校验,保持现状)。
- auth_enabled=True:订单必须属于当前登录用户;拿不到当前用户 → 拒
  (fail-closed,订单是隐私,与优惠券 query_coupons 的 fail-open 相反)。
"""

from __future__ import annotations


def owned_order(order_id: str):
    """取单并做归属校验;不存在/越权/无身份(auth 开)→ None。"""
    from app.db import get_db
    order = get_db().get_order(order_id)
    if order is None:
        return None
    from app.config.settings import settings
    if not settings.auth_enabled:
        return order
    from app.agent.runtime_context import get_current_user
    uid = get_current_user()
    if not uid:
        return None
    return order if order.get("user") == uid else None
