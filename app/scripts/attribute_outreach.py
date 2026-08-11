"""触达转化归因 worker(N3):发送时记基线,到期判定买家有没有真的往前走。

  python -m app.scripts.attribute_outreach --once

判定是**确定性**的,不经模型:目标订单状态从发送那一刻的 `status_at_send`
向前推进(`unpaid → pending → shipped → delivered` 的序)才算 `converted`;
状态倒退(比如退款)或原地不动都是 `no_change`——这与本项目"阈值判定可复现,
不问模型"的一贯口径一致(参见 anomaly.py 的确定性异常扫描)。

幂等的关键在数据,不在这里的控制流:`pending_attribution` 只挑 `outcome=
'pending'` 的行,`set_outreach_outcome` 又是条件更新(只对仍是 pending 的行
生效)。一条草稿被判过一次之后,第二次调用 `attribute_once` 天然看不到它,
不需要额外记"这条处理过没有"。
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from app.db import get_db
from app.db.database import Database

logger = logging.getLogger(__name__)

# 状态推进序:下标越大代表越靠后。unpaid_flow_enabled 关闭时订单从 pending
# 起步、永远不会落到 unpaid,这份序依然成立——unpaid 只是从此用不到的最前一格,
# 不需要为开关状态另起一份序。
STATUS_ORDER = ["unpaid", "pending", "shipped", "delivered"]

# "判不出来"这一档。与 converted / no_change 并列而不是折进后者——见 `_judge`
# 的说明:no_change 会进 conversion_rates 的分母,把无法判断当成失败会永久
# 拉低该商机类型的历史转化率。
OUTCOME_UNATTRIBUTABLE = "unattributable"


def _progressed(before: str, after: str) -> bool:
    """判定 after 相对 before 是否**向前推进**(而不仅仅是"不同")。

    未知状态值(不在 STATUS_ORDER 里,比如 refund_processing)一律判"没有
    推进"——保守:判不清楚就不算数,不会把退款之类的侧向变化误记成转化。
    """
    if before not in STATUS_ORDER or after not in STATUS_ORDER:
        return False
    return STATUS_ORDER.index(after) > STATUS_ORDER.index(before)


def _judge(draft: dict, db: Database) -> str:
    """对一条到期草稿判定 converted / no_change / unattributable。

    有 order_id:目标订单状态从 status_at_send 是否向前推进。
    无 order_id(弃单、咨询未下单这类商机):看该买家是否在发送后新建了订单——
    这类商机本来就没有"一单的状态"可比,唯一能确定性观测到的"往前走"就是
    "买家下单了"。

    **`unattributable` 是"判不出来",不是"判出来是坏的"。** 改造前这两种情况都
    返回 `no_change`,而 `no_change` 不是中性值——`priority.conversion_rates` 把
    `outcome IN ('converted','no_change')` 当分母,于是一条**无法判断**的触达会被
    当成一次**失败**的触达,永久拉低该商机类型的历史转化率,而那个转化率占商机
    打分权重 0.3(`priority_weight_conversion`)。判不出来却被记成失败,是这个
    代码库反复出现的同一类错(参见 anomaly 的 `service_insufficient`、
    service_quality 的 `other` 桶)。

    两种判不出来:
      - 订单查不到了(被删/被归档):没有可比对的当前状态;
      - `status_at_send` 为空:没有基线。`_progressed("", x)` 因为空串不在
        STATUS_ORDER 里恒返回 False,于是这条草稿**无论买家做什么都会被判
        no_change**——实测库里 4 条已发送草稿里有 3 条正是这种(它们关联的
        DEMO-006/008/010 在 orders 表里不存在)。

    `conversion_rates` 不需要任何改动:它的 WHERE 本来就是白名单,新增的取值
    自动被排除在分母之外。
    """
    order_id = (draft.get("order_id") or "").strip()
    if order_id:
        order = db.get_order(order_id)
        if order is None:
            return OUTCOME_UNATTRIBUTABLE
        baseline = (draft.get("status_at_send") or "").strip()
        if not baseline:
            return OUTCOME_UNATTRIBUTABLE
        return "converted" if _progressed(baseline,
                                          order.get("status") or "") else "no_change"

    user_id = draft.get("user_id") or ""
    sent_at = draft.get("sent_at") or ""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM orders WHERE user = ? AND created_at > ? LIMIT 1",
            (user_id, sent_at)).fetchone()
        return "converted" if row is not None else "no_change"
    finally:
        conn.close()


def attribute_once(window_hours: Optional[int] = None, db: Optional[Database] = None) -> dict:
    """跑一轮归因:捞到期草稿逐条判定,写结果并回发总线。

    返回 `{"checked": int, "converted": int, "no_change": int}`——`checked`
    只计"这次调用真的写成功了判定结果"的行数(`set_outreach_outcome` 的
    条件更新拿到 rowcount>0),并发/重跑抢不到的行不计入,`second["checked"]
    == 0` 正是这一点的直接体现。

    发布到总线是 fail-soft:`bus.publish` 内部已经把异常吞掉返回 None,这里
    不额外兜底也不会让判定结果本身丢失或错乱——写库(`set_outreach_outcome`)
    发生在发布之前且已经 commit,总线故障顶多让这条草稿的时间线上少一跳。
    """
    from app.multi_agent import bus

    from app.config.settings import settings

    d = db or get_db()
    hours = window_hours if window_hours is not None else settings.outreach_attribution_window_hours

    stats = {"checked": 0, "converted": 0, "no_change": 0,
             # 判不出来的条数。**必须报出来**:它不进转化率分母(见 `_judge`),
             # 所以不单独计数的话这些草稿就从所有统计里彻底消失了——那与把它们
             # 记成失败是两种相反的错,都不可接受。这个数持续不为 0,就该去查
             # 为什么草稿关联的订单查不到、或发送时没落上状态基线。
             OUTCOME_UNATTRIBUTABLE: 0}
    pending = d.pending_attribution(older_than_hours=hours)
    for draft in pending:
        outcome = _judge(draft, d)
        if not d.set_outreach_outcome(draft["id"], outcome):
            # 没抢到(已被别的进程/上一轮处理过):不重复计数,这正是幂等的体现。
            continue
        stats["checked"] += 1
        stats[outcome] += 1

        if outcome == OUTCOME_UNATTRIBUTABLE:
            # 不发总线事件:没有"结果"可回写给参谋——发一条 no_change 会让下游
            # 统计到一次并不存在的失败,而总线上也没有为"判不出来"设计的收件人。
            # 出口是上面那个计数 + 这条 warning,不是静默跳过。
            logger.warning(
                "触达归因判不出来(不计入转化率) draft=%s order=%s baseline=%r"
                "——通常是关联订单已不存在,或发送时没落上状态基线",
                draft.get("id"), draft.get("order_id"), draft.get("status_at_send"))
            continue

        # 事件类型常量归 app/multi_agent/bus.py 唯一持有(与 EV_SIGNAL_ANOMALY
        # 等其它事件类型同放一处),这里只取用,不再另起一份。
        event_type = bus.EV_OUTREACH_CONVERTED if outcome == "converted" else bus.EV_OUTREACH_NO_CHANGE
        bus.publish(
            event_type,
            {"draft_id": draft["id"], "user_id": draft.get("user_id"),
             "order_id": draft.get("order_id"), "outcome": outcome,
             "status_at_send": draft.get("status_at_send")},
            bus.AGENT_ANALYST,
            correlation_id=draft.get("correlation_id") or None,
        )
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="触达转化归因 worker")
    parser.add_argument("--once", action="store_true", help="跑一轮归因")
    parser.add_argument("--window-hours", type=int, default=None,
                        help="归因窗口小时数,默认取配置 outreach_attribution_window_hours")
    args = parser.parse_args(argv)

    if not args.once:
        parser.print_help()
        return 2

    stats = attribute_once(window_hours=args.window_hours)
    print(f"[attribute] checked={stats['checked']} converted={stats['converted']} "
          f"no_change={stats['no_change']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
