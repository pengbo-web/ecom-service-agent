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

另一个已知边界(user_id → session_id 这一跳上的):`merge_user_conversations`
(app/api/conversations.py)会在检测到某用户有多条碎片 open 会话时,把规范
会话**换成**另一条(取 latest_conversation 那条),并 close 掉旧的。如果
"正在接管"这个状态是在旧的规范会话上进入的(`hitl.manual_mode` 记的是旧
session_id),而合并恰好发生在此之后,仲裁这里按 user_id 查到的是**新**
canonical id,`is_manual(新 sid)` 查到的是 False——旧 id 上那次接管就会被
错过,判定为可发。未结工单这条路不受影响:工单本身按 session_id 持久存,
即使合并换了规范会话,原工单的 session_id 不变,`list_pending()` 里那一条
仍在,仍能拦下。也就是说这个缺口只在"接管进行中但还没升级出工单"的窄
时间窗内成立,且只覆盖一半(接管态),不覆盖另一半(工单)。这是继承下来的
设计边界,不在本次改动范围内;修复需要让 `ManualMode` 随合并重新挂到新
session_id 上,是另一件有自己风险的事,这里只记录、不处理。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

BLOCK_MANUAL = "manual_takeover"
BLOCK_OPEN_HANDOFF = "open_handoff"
BLOCK_UNKNOWN = "arbitration_failed"


def check_outreach_allowed(user_id: str, hitl=None,
                           db=None) -> tuple[bool, str, str]:
    """该买家当下是否可以接收营销触达。返回 (allowed, code, 中文原因)。

    `code` 是机器可读的拒绝原因,取值见模块常量 `BLOCK_MANUAL` /
    `BLOCK_OPEN_HANDOFF` / `BLOCK_UNKNOWN`;放行时固定为 `""`。加它是为了让
    调用方(审批端点)能把"仲裁拒绝"这件事本身、以及拒绝的具体类型,做成一个
    稳定字段透出去,而不必靠中文文案做字符串匹配去区分"人工接管"与"未结
    工单"与"查不清状态"——后三者对店主而言都只是"没发出去",但对写日志/
    做统计的一方是三件不同的事。

    **user_id → session_id 这一跳是本函数唯一容易错的地方**:草稿记的是
    user_id(营销面向"人"),而接管态与工单挂在 session_id 上,所以必须先由
    user_id 找到该买家当前会话,再查那个会话的状态。

    没有会话的买家(新客)判可发——他谈不上"正在投诉",拿这条闸拦他会让
    营销永远碰不到新客。

    fail-closed 覆盖整个函数体,不只是"查接管态/工单"这一步:`get_db()`
    默认路径本身也可能抛(比如打不开库文件),所以它和后续的查询一样被纳入
    同一个 try——本函数没有任何一条出路允许异常穿透出去,调用方
    (`approve_draft`)外面没有包 try/except,异常逃逸出去就是操作者看到一个
    裸 500,而不是"已拒绝发送"这个更安全的结果。
    """
    uid = (user_id or "").strip()
    if not uid:
        return False, BLOCK_UNKNOWN, "草稿没有目标买家,无法确认其当前状态,已拒绝发送。"
    if hitl is None:
        return True, "", ""      # 该部署没开 HITL,不存在"正在投诉"这个状态

    try:
        if db is None:
            from app.db import get_db
            db = get_db()

        conv = db.latest_conversation(uid)
        if not conv:
            return True, "", ""
        sid = conv.get("conversation_id") or ""
        if not sid:
            return True, "", ""

        if hitl.manual_mode.is_manual(sid):
            return False, BLOCK_MANUAL, ("该买家的会话正由人工客服接管中,"
                           "此时发营销消息会打断人工处理,已拒绝发送。")

        queue = getattr(hitl, "queue", None)
        if queue is not None:
            pending = queue.list_pending() or []
            if any((p.get("session_id") or "") == sid for p in pending):
                return False, BLOCK_OPEN_HANDOFF, ("该买家有未结的人工工单(正在等处理),"
                               "此时发营销消息不合适,已拒绝发送。")
    except Exception as exc:  # noqa: BLE001 fail-closed:查不清一律不发
        logger.exception("触达仲裁查询失败,按不可发处理 user=%s: %s", uid, exc)
        return False, BLOCK_UNKNOWN, "无法确认该买家当前是否在人工处理中,出于谨慎已拒绝发送。"

    return True, "", ""
