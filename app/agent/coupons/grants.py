"""优惠券发放:唯一入口 `issue_for_draft`,只能由审批端点(app/api/app.py 的
`approve_draft`)在人工点过批准之后调用。

Global Constraint 1:发放优惠券碰的是真金白银,而且不可撤销——本模块**不
注册成任何 Agent 工具**(不出现在 app/agent/tools/registry.py 的 `_TOOL_MAP`
里),模型只能通过 `growth.draft_outreach` 把想建议的券码写进草稿的 offer
字段,发放本身只在人工审批之后、这一处函数里才真的发生。

`KNOWN_CODES` 从 `order_ops._COUPONS` **派生**,不另建一份券定义——那份列表
是店铺实际在售的券的唯一事实来源。券码必须在其中才允许发放:模型编一个
不存在的券码、人工审批时又没注意到,正是这道闸要挡住的失败。
"""

from __future__ import annotations

from app.agent.tools.order_ops import _COUPONS
from app.db import get_db

# 唯一从 _COUPONS 派生,不再抄一份券码/文案表——这份列表已经在本项目里
# 因为手抄表漏同步坑过好几次(见 growth.py/app.py 的相关注释)。
KNOWN_CODES: set[str] = {c["code"] for c in _COUPONS}

# code -> 完整券定义(name/discount/expires/audience),供审批端点在人工点下
# 批准**之前**把券的文案(如"满300减30")显示出来——同样派生自 _COUPONS,
# 不另建一份。
COUPON_BY_CODE: dict[str, dict] = {c["code"]: c for c in _COUPONS}


def issue_for_draft(draft: dict, granted_by: str) -> tuple[bool, str]:
    """按草稿 `offer.coupon_code` 发放优惠券。返回 (是否可以继续投递, 原因)。

    - 草稿没带 `coupon_code`:视为无券草稿,直接放行(True, "")——这不是
      失败,只是这条草稿本来就不含优惠券。
    - `coupon_code` 不在 `KNOWN_CODES` 里:拒绝(False, ...)。这是模型编造
      券码、人工审批时没注意到的那个失败场景,必须在这里挡住,而不是等
      发出去了才发现店铺根本没有这张券。
    - 已经给同一买家发过这张券:`grant_coupon` 命中 `UNIQUE(code, user_id)`
      返回 None,同样判为失败——防止重复发放同一张券。
    """
    offer = draft.get("offer") or {}
    code = (offer.get("coupon_code") or "").strip()
    if not code:
        return True, ""
    if code not in KNOWN_CODES:
        return False, f"券码「{code}」不是本店在售的优惠券,已拒绝发放"

    user_id = draft.get("user_id", "")
    draft_id = draft.get("id")
    reason = f"营销触达赠券(草稿 {draft_id})"
    gid = get_db().grant_coupon(code, user_id, draft_id, reason, granted_by)
    if gid is None:
        return False, f"买家 {user_id} 已被发放过优惠券「{code}」,不能重复发放"
    return True, ""
