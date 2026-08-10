"""商机优先级打分:确定性排序,不经模型、不需要新埋点。

解决的问题:`find_opportunities` 原本按 `created_at DESC` 取前 N 条——也就是
**最新的**那批,而"最新"恰恰意味着**滞留最短**。真正该先催的那些(拖了一周的
大额未支付单)反而因为不够新被 LIMIT 切掉了,顺序上也排在最后。店主看到的是
一份按时间倒序的流水,不是一份按该先做什么排的清单。

打分只用**已经在库里的三样东西**,不引入任何新采集:

  1. 滞留时长 stale_hours —— 拖得越久越该催,到 `priority_stale_saturation_hours`
     封顶(拖了三个月和拖了半年没有区别,都是最高档;不封顶会让极老的僵尸单
     永远霸占榜首)。
  2. 订单金额 amount —— 金额越大越该先花那一次触达,到 `priority_amount_cap`
     封顶(同理:一单 5 万和一单 50 万,对"要不要先催"这件事没有量级差异)。
  3. 该类商机的历史转化率 —— 来自 `outreach_drafts.outcome`(归因 worker 写的
     converted / no_change),按 opportunity_type 聚合。样本不足
     (< `priority_min_samples`)时用先验值 `priority_conversion_prior`,不拿
     "1 单转 1 单 = 100%" 当真——与 anomaly_scan 的 `anomaly_min_samples` 同一条
     纪律。

**三项都是可解释的事实,不是模型判断。** 每条商机都带 `priority_reason`,店主
能一眼看出为什么它排第一;排序结果可复算、可争论,这是"确定性"真正的价值——
一个模型给的分数,店主质疑时你无法回答"为什么它比那条高"。

刻意没做的:
- **没有把"最佳触达时机"做成时间窗模型**(比如"周三下午打开率最高")。那需要
  打开/回复埋点,本项目没有,硬编一套行业经验值只是把猜测包装成算法。
  这里做的是**优先级排序**——同样服务于"先跟谁",但每一分都能追到一行真实数据。
- 没有做买家粒度的意向打分:那需要行为序列(浏览/停留/加购路径),同样没有埋点。
"""

from __future__ import annotations

import logging

from app.config.settings import settings

logger = logging.getLogger(__name__)

#: 归因 worker 写进 outreach_drafts.outcome 的两个终态(pending 不计入分母——
#: 还没到判定窗口的触达既不算成功也不算失败,计进去会把新上线的商机类型
#: 系统性地压低)。
_OUTCOME_CONVERTED = "converted"
_OUTCOME_NO_CHANGE = "no_change"


def conversion_rates(db) -> dict[str, tuple[float, int]]:
    """按商机类型统计历史转化率。返回 {kind: (rate, samples)}。

    只统计已判定的触达(outcome 已经不是 pending)。读库失败返回空 dict——
    调用方会退化成对所有类型用先验值,顺序仍然可用,只是少了一个维度。
    """
    try:
        conn = db.connect()
    except Exception:  # noqa: BLE001 打分是增强,连不上库不能让找商机失败
        logger.warning("读取历史转化率失败(本轮用先验值)", exc_info=True)
        return {}
    try:
        rows = conn.execute(
            "SELECT opportunity_type AS kind, "
            "       SUM(CASE WHEN outcome = ? THEN 1 ELSE 0 END) AS converted, "
            "       COUNT(*) AS total "
            "FROM outreach_drafts WHERE outcome IN (?, ?) "
            "GROUP BY opportunity_type",
            (_OUTCOME_CONVERTED, _OUTCOME_CONVERTED, _OUTCOME_NO_CHANGE)).fetchall()
        out: dict[str, tuple[float, int]] = {}
        for r in rows:
            total = int(r["total"] or 0)
            if total <= 0:
                continue
            out[str(r["kind"])] = (float(r["converted"] or 0) / total, total)
        return out
    except Exception:  # noqa: BLE001
        logger.warning("读取历史转化率失败(本轮用先验值)", exc_info=True)
        return {}
    finally:
        conn.close()


def _weights() -> tuple[float, float, float]:
    """三项权重,归一化成和为 1。

    归一化是为了让分数恒在 0..1:店主把某一项权重从 0.3 调到 3.0 时,意图显然是
    "这项更重要",而不是"让总分溢出到 3.7 分"。和为 0(全填 0)时退回等权,
    而不是除零。
    """
    w = (max(0.0, settings.priority_weight_stale),
         max(0.0, settings.priority_weight_amount),
         max(0.0, settings.priority_weight_conversion))
    total = sum(w)
    if total <= 0:
        return (1 / 3, 1 / 3, 1 / 3)
    return (w[0] / total, w[1] / total, w[2] / total)


def score_opportunity(item: dict, rates: dict[str, tuple[float, int]]) -> tuple[float, str]:
    """给一条商机打分,返回 (0..1 的分数, 给人看的理由)。

    纯函数:不读库、不看时钟——`stale_hours` 由 SQL 用**选中这一行的那个时钟**
    算好带过来(见 growth.py 的 `_STALE_HOURS`)。这一点不是洁癖:本项目的
    `Database._now()` 写的是本地时间,而商机 SQL 的 WHERE 用的是 SQLite 的
    `datetime('now')`(UTC),在 Python 侧再算一次时间差会与"筛出这一行的那个
    条件"自相矛盾——一条刚好卡在阈值边缘的订单,可能被 WHERE 判为"已滞留 25h"
    却被打分判成"滞留 -7h",分数直接归零。
    """
    w_stale, w_amount, w_conv = _weights()

    hours = item.get("stale_hours")
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        hours = 0.0
    sat = max(1.0, float(settings.priority_stale_saturation_hours))
    stale_part = min(max(hours, 0.0) / sat, 1.0)

    raw_amount = item.get("amount")
    cap = max(1.0, float(settings.priority_amount_cap))
    if raw_amount is None or (isinstance(raw_amount, (int, float)) and raw_amount <= 0):
        # 金额未知(咨询未下单/议价未成交这类商机本就没有订单金额)按中性值,
        # **不按 0**:按 0 等于断言"这个买家不值钱",而事实只是"这条数据里没有
        # 金额"。把"缺失"当成"最差"是排序里最常见的一类偏见。
        amount_part = min(max(float(settings.priority_unknown_amount), 0.0), 1.0)
        amount_desc = "金额未知"
    else:
        amount_part = min(float(raw_amount) / cap, 1.0)
        amount_desc = f"¥{float(raw_amount):.0f}"

    kind = str(item.get("kind") or "")
    rate, samples = rates.get(kind, (None, 0))
    if rate is None or samples < settings.priority_min_samples:
        conv_part = min(max(float(settings.priority_conversion_prior), 0.0), 1.0)
        conv_desc = f"历史转化样本不足({samples})"
    else:
        conv_part = min(max(rate, 0.0), 1.0)
        conv_desc = f"该类历史转化 {rate:.0%}(样本 {samples})"

    score = w_stale * stale_part + w_amount * amount_part + w_conv * conv_part
    reason = f"滞留 {hours:.0f}h · {amount_desc} · {conv_desc}"
    return round(score, 4), reason


def rank(items: list[dict], rates: dict[str, tuple[float, int]]) -> list[dict]:
    """按优先级降序排序,并把 priority_score / priority_reason 挂回每一条。

    就地改 dict 再返回新列表:调用方(find_opportunities)拿到的就是排好序、
    带着理由的同一批商机对象,不需要第二次遍历去贴字段。

    并列时按 stale_hours 降序兜底,保证同分下顺序稳定可复现(不同 Python 版本
    的字典/排序实现不该影响店主看到的名单顺序)。
    """
    for it in items:
        score, reason = score_opportunity(it, rates)
        it["priority_score"] = score
        it["priority_reason"] = reason
    return sorted(items,
                  key=lambda x: (x.get("priority_score") or 0.0,
                                 float(x.get("stale_hours") or 0.0)),
                  reverse=True)
