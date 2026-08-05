"""跨 Agent 冲突仲裁:阻止一个 Agent 的动作撞上另一条链正在处理的事。

当前唯一一条真实冲突:营销 Agent 的触达草稿是"退款率异常"这条协作链产的,
与某个具体买家**当下的状态**无关。于是一个正在投诉、会话已转人工接管的顾客,
照样可能收到"这款鞋不少朋友反馈偏大,需要帮您确认吗"。

collab.py 里已经挡了"escalation 信号不触发营销",但那只管**信号侧**:草稿不是
由这个买家的升级产生的,所以那道闸对它不生效。仲裁必须放在**投递侧**。

fail-closed:查不清状态一律判不可发。这条闸守的是"别给正在投诉的人推销",
查不清就不发的代价远小于发错。

已知边界:`ManualMode` 是内存态且带超时回落,所以进程重启后"正在接管"这个
信息会丢,此时仲裁只能靠未结工单兜。工单持久在独立 SQLite,是更可靠的那一半。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

BLOCK_MANUAL = "manual_takeover"
BLOCK_OPEN_HANDOFF = "open_handoff"
BLOCK_UNKNOWN = "arbitration_failed"


def check_outreach_allowed(user_id: str, hitl=None,
                           db=None) -> tuple[bool, str]:
    """该买家当下是否可以接收营销触达。返回 (allowed, 中文原因)。

    **user_id → session_id 这一跳是本函数唯一容易错的地方**:草稿记的是
    user_id(营销面向"人"),而接管态与工单挂在 session_id 上,所以必须先由
    user_id 找到该买家当前会话,再查那个会话的状态。

    没有会话的买家(新客)判可发——他谈不上"正在投诉",拿这条闸拦他会让
    营销永远碰不到新客。
    """
    uid = (user_id or "").strip()
    if not uid:
        return False, "草稿没有目标买家,无法确认其当前状态,已拒绝发送。"
    if hitl is None:
        return True, ""      # 该部署没开 HITL,不存在"正在投诉"这个状态

    if db is None:
        from app.db import get_db
        db = get_db()

    try:
        conv = db.latest_conversation(uid)
        if not conv:
            return True, ""
        sid = conv.get("conversation_id") or ""
        if not sid:
            return True, ""

        if hitl.manual_mode.is_manual(sid):
            return False, ("该买家的会话正由人工客服接管中,"
                           "此时发营销消息会打断人工处理,已拒绝发送。")

        queue = getattr(hitl, "queue", None)
        if queue is not None:
            pending = queue.list_pending() or []
            if any((p.get("session_id") or "") == sid for p in pending):
                return False, ("该买家有未结的人工工单(正在等处理),"
                               "此时发营销消息不合适,已拒绝发送。")
    except Exception as exc:  # noqa: BLE001 fail-closed:查不清一律不发
        logger.exception("触达仲裁查询失败,按不可发处理 user=%s: %s", uid, exc)
        return False, "无法确认该买家当前是否在人工处理中,出于谨慎已拒绝发送。"

    return True, ""
