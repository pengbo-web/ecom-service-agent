"""跟进序列(N7:持续沟通):到期自动推进,产物仍是待审草稿。

一次营销触达不是"发一条就完事":买家没反应,合理的下一步是隔一段时间再提一次,
但提几次、什么时候该停,不能一直靠人盯着。这里自动化的只是**调度**——判断
一条链是否到期、要不要继续、继续的话起草这一步的话术——**不是发送**。发送
仍然只发生在管理端的审批端点里,且必须有人点过"批准",与 growth.py 的
`draft_outreach` 是同一条纪律:营销侧任何函数都不得碰发送通道。

终止条件全部确定性、全部由已落库的状态判定,不问模型:
1. 该买家该 kind 的商机已经消失(已支付/已下单/购物车已转化)→ `_opportunity_still_open`
2. 上一条触达已被归因 worker 判定为 `converted` → `_last_touch_converted`
3. `check_outreach_allowed` 判不可发(人工接管中/有未结工单)→ **复用现有仲裁,
   不新建判断**——这是本项目唯一一处"该买家当下能不能被联系"的权威判定,
   第二次实现同一件事只会让两处口径慢慢走岔。
4. 达到 `max_steps` → 数据层(`Database.advance_followup`)直接收尾为 `done`,
   不经过这里的三条判定(达到步数上限不是"被什么原因拦下",是正常走完)。
5. 同一买家同一 kind 只能有一条 active 链 → 数据层唯一索引兜底
   (`Database.start_followup` 的 IntegrityError → None),不是这里的判断,
   因为"防两条链并行"必须是第二个调用方也躲不开的约束,不能只靠调用者
   自觉守规矩。

条件 1/2/3 里,3 的优先级最高、无条件先判:买家正被人工处理中这件事,不该
因为"商机技术上还开着"或"上次触达还没判定"而被绕过——接管中就是接管中,
与商机状态无关。
"""

from __future__ import annotations

import logging
from typing import Optional

from app.db import get_db
from app.multi_agent import bus
from app.multi_agent.arbitration import check_outreach_allowed

logger = logging.getLogger(__name__)

MAX_STEPS = 3
STEP_INTERVAL_HOURS = 48

# 终止原因 → 中文,店主要能看懂"为什么不再跟了"(约束:链停下时必须可见原因)。
STOP_REASON_LABELS = {
    "converted": "商机已消失或上一次触达已经生效,无需再跟进",
    "in_service": "该买家正由人工处理中(接管中/有未结工单),已停止自动跟进",
}


def _opportunity_still_open(row: dict, db) -> bool:
    """按 kind 查真实数据,判该买家这类商机是否还开着——全部是确定性的
    状态匹配,不猜测、不经模型。

    - unpaid_order:该买家名下是否还有 status='unpaid' 的订单(已支付则消失)
    - abandoned_cart:该买家名下是否还有 status='active' 的购物车行
      (已转化下单或已移除则消失)
    - stale_pending_order:该买家名下是否还有 status='pending' 的订单
      (已发货/已签收/退款都算"不再是久拖不发这件事")
    - stalled_bargain / consulted_no_order:这两类商机的"已消失"就是
      "买家下单了"——一旦名下出现任意订单即视为转化,不再是"没下单"这件事
    - shipped_no_care:该买家名下是否还有 status='shipped' 的订单——一旦
      推进到 delivered(或转退款等其它终态),"发货未关怀"这件事本身就已经
      不成立,继续提物流播报只会显得莫名其妙(包裹都签收了还在说"已发货")
    - delivered_no_review:复用 `Database.reviewable_items`——只要该买家
      名下还有"已签收且未评价"的条目就算商机仍开着;买家一旦评价完,
      reviewable_items 返回空列表,链就该停,不能评完了还在收到邀评提醒
      (这正是本函数存在的目的:防止这类"事后确定性会消失"的商机被继续骚扰)
    """
    kind = row.get("kind")
    user_id = row.get("user_id")
    if kind == "delivered_no_review":
        return bool(db.reviewable_items(user_id))
    conn = db.connect()
    try:
        if kind == "unpaid_order":
            r = conn.execute(
                "SELECT 1 FROM orders WHERE user = ? AND status = 'unpaid' LIMIT 1",
                (user_id,)).fetchone()
            return r is not None
        if kind == "abandoned_cart":
            r = conn.execute(
                "SELECT 1 FROM carts WHERE user_id = ? AND status = 'active' LIMIT 1",
                (user_id,)).fetchone()
            return r is not None
        if kind == "stale_pending_order":
            r = conn.execute(
                "SELECT 1 FROM orders WHERE user = ? AND status = 'pending' LIMIT 1",
                (user_id,)).fetchone()
            return r is not None
        if kind == "shipped_no_care":
            r = conn.execute(
                "SELECT 1 FROM orders WHERE user = ? AND status = 'shipped' LIMIT 1",
                (user_id,)).fetchone()
            return r is not None
        # stalled_bargain / consulted_no_order
        r = conn.execute(
            "SELECT 1 FROM orders WHERE user = ? LIMIT 1", (user_id,)).fetchone()
        return r is None
    finally:
        conn.close()


def _last_touch_converted(row: dict, db) -> bool:
    """本条链上一次触达(草稿)是否已被归因 worker(attribute_outreach.py)
    判定为 converted。草稿与链靠 correlation_id 对上——每一步产的草稿都带着
    这条链自己的 correlation_id(见 `run_due`),不是各自新起一条。"""
    corr = row.get("correlation_id") or ""
    if not corr:
        return False
    conn = db.connect()
    try:
        r = conn.execute(
            "SELECT outcome FROM outreach_drafts WHERE correlation_id = ? "
            "ORDER BY id DESC LIMIT 1", (corr,)).fetchone()
        return bool(r) and r["outcome"] == "converted"
    finally:
        conn.close()


def should_stop(fid_row: dict, db=None, hitl=None) -> tuple[bool, str]:
    """判定这条到期链本轮该不该被终止。返回 (是否终止, 终止原因码)。

    终止原因码只有两个取值:`"converted"`(商机消失/上次触达已生效)、
    `"in_service"`(仲裁拒绝——人工接管中/有未结工单)。达到 `max_steps` 不
    经过这里,由 `Database.advance_followup` 在数据层直接收尾为 `done`。
    """
    d = db or get_db()

    # 仲裁无条件先判:买家正被人工处理中,与商机状态无关,不能被绕过。
    allowed, _code, _reason = check_outreach_allowed(
        fid_row.get("user_id"), hitl=hitl, db=d)
    if not allowed:
        return True, "in_service"

    if not _opportunity_still_open(fid_row, d):
        return True, "converted"

    if _last_touch_converted(fid_row, d):
        return True, "converted"

    return False, ""


def _compose_followup_text(row: dict, db) -> str:
    """按当前 step 拼这一轮的跟进话术。不经模型:纯模板拼接——跟进序列的
    价值在于**调度**,不在于话术花样;每一步仍要人工审批,模板化文案降低了
    "AI 写歪了"的残余风险,而不会让人工少看一眼。"""
    from app.agent.tools.growth import OPPORTUNITY_KINDS

    label = OPPORTUNITY_KINDS.get(row.get("kind"), row.get("kind") or "")
    step = int(row.get("step") or 1)
    return f"这是第 {step} 次跟进提醒:您关注的「{label}」还没有完成,需要帮您处理一下吗?"


def run_due(limit: int = 20, hitl=None, db=None) -> dict:
    """跑一轮到期跟进:每条先判三条动态终止条件(见 `should_stop`),该停的
    落 stop_reason 停掉;没停的产一条新草稿(**仍是待审,绝不发送**),挂上
    这条链自己的 correlation_id,再推进一步(可能因达到 max_steps 收尾为 done)。

    起草直接写 `db.create_outreach_draft`,不经 `growth.draft_outreach`——
    后者内部固定调用全局 `get_db()`,而这里的 `db` 可能是调用方注入的另一个
    实例(worker/测试都需要这个自由度);但commitment 敏感词分级复用
    `growth._trim_and_classify` 这份唯一口径,不重写一遍判断。
    """
    from app.agent.tools.growth import _trim_and_classify

    d = db or get_db()
    due = d.due_followups(limit=limit)
    stats = {"checked": 0, "drafted": 0, "stopped": 0, "done": 0}

    for row in due:
        stats["checked"] += 1
        stop, reason = should_stop(row, db=d, hitl=hitl)
        if stop:
            if d.stop_followup(row["id"], reason):
                stats["stopped"] += 1
            continue

        text = _compose_followup_text(row, d)
        clean, review_reason = _trim_and_classify(text)
        if clean:
            corr = row.get("correlation_id") or bus.new_correlation_id("FOLLOWUP")
            d.create_outreach_draft(
                opportunity_type=row.get("kind"), user_id=row.get("user_id") or "",
                order_id="", content=clean, offer={},
                reason=f"跟进序列第 {row.get('step')} 步自动推进",
                correlation_id=corr, created_by=bus.AGENT_GROWTH,
                needs_review_reason=review_reason)
            stats["drafted"] += 1
        else:
            logger.warning("跟进链 %s 本轮拼出的话术为空,跳过起草仅推进步数",
                           row.get("id"))

        d.advance_followup(row["id"])
        updated = d.get_followup(row["id"])
        if updated and updated.get("status") == "done":
            stats["done"] += 1

    return stats
