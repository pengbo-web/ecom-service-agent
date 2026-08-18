"""业务效果报表:这套客服系统在**真实流量**上表现如何。

补的是评估体系里一个结构性缺口:`evaluator.py` 产出的是
`pass_rate / avg_process_score / avg_result_score / total_tokens` —— 那是**模型与
流程的质量**,不是**客服业务的效果**。运营看的第一张报表从来是自助解决率、转人工率、
平均轮次、差评率。

---

**为什么这是独立的一份报表,而不是往 `evaluator.py` 里加字段。**

沙箱评测跑的是 10 条固定用例,而且刻意关掉了记忆与 MCP 以保可复现(见
`sandbox.py`)。那个环境里**没有真实用户**:算不出转人工率(没人会不满),算不出
自助解决率(用例本来就都跑完),算不出会话轮次(轮数是用例写死的)。

两份报表回答两个不同的问题:

    evaluator.py       改了 Prompt/Skill 之后,能力有没有退步?     ← 沙箱、可复现、可门禁
    business_metrics   这套系统在真实流量上到底好不好用?          ← 生产数据、不可复现

硬塞进同一份报告会让"沙箱里的 10 条用例"和"线上的几百轮真实对话"共用一个分母,
那是本项目最不该犯的一类错误。

---

**只认真实流量,而且把过滤前后的差距摆出来。**

复用 `runtime_context.DECISION_SOURCES`(= `{live}`),与看门狗的判定口径**同一处
常量**。理由是本仓库实测过的一次事故形态:`track-order` 全量成功率 28%,而其中
41 条来自压测与走查,真实买家只有 17 条、成功率 88% —— 看门狗差点据此把一个正常
的 skill 撤下线。

业务指标犯同一个错的代价更隐蔽:它不会触发任何自动动作,只会让人**对系统形成一个
错的判断**,然后据此去优化不存在的问题。所以这份报表同时给出 `live` 与全量两套数,
并算出失真幅度 —— 让"过滤这件事到底有多重要"变成一个可以被看见的数字,而不是一句
方法论主张。

---

**样本不足要显式说出来,不输出一个无意义的数。**

与 `anomaly_scan` 的 `service_insufficient`、门禁的 `evaluable=False` 同一条纪律:
1 条评价算出的差评率不是 0% 或 100%,是**没有数据**。凑出来的比率会被写进简历、被
拿去汇报,而它什么都不代表。低于 `min_samples` 的指标一律进 `insufficient` 列表,
值置 None。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: 差评判定阈值。**不在这里另写一个数**——与 `agent/tools/reviews.py` 的
#: `BAD_RATING_MAX`、`anomaly_scan` 的差评率判定共用同一口径,三处各写一遍必然漂移。
from app.agent.tools.reviews import BAD_RATING_MAX  # noqa: E402

#: 轮级结局的三种取值(`skill_traces.outcome`)。`handoff` 是转人工,
#: `tool_error` 是工具失败,`success` 是这一轮自助完成。
OUTCOME_SUCCESS = "success"
OUTCOME_TOOL_ERROR = "tool_error"
OUTCOME_HANDOFF = "handoff"


def _rate(part: int, whole: int) -> Optional[float]:
    """比率;分母为 0 返回 None 而不是 0.0。

    0 和"没有数据"在业务报表上是两件完全不同的事:前者是"一次都没发生过"(好消息),
    后者是"这个指标算不出来"。混成 0.0 会让空库看起来像满分。
    """
    return (part / whole) if whole else None


def _turn_level(db, sources: Optional[set], window_days: Optional[int]) -> dict:
    """轮级指标:一轮对话 = `skill_traces` 的一行。

    `sources=None` 表示不过滤(全量对照用)。
    """
    sql = ("SELECT outcome, COUNT(*) AS n FROM skill_traces WHERE 1=1")
    params: list = []
    if sources:
        # COALESCE:历史行的 source 可能为 NULL(那批数据早于这个字段),
        # 按 'unknown' 参与比较而不是被 NULL 比较静默丢掉。
        sql += " AND COALESCE(source, 'unknown') IN (%s)" % ",".join("?" * len(sources))
        params += sorted(sources)
    if window_days:
        from app.db import dialect
        sql += f" AND created_at >= {dialect.now_minus(int(window_days), 'days')}"
    sql += " GROUP BY outcome"

    conn = db.connect()
    try:
        counts = {str(dict(r)["outcome"]): int(dict(r)["n"])
                  for r in conn.execute(sql, tuple(params)).fetchall()}
    finally:
        conn.close()

    total = sum(counts.values())
    return {
        "turns": total,
        "self_served": counts.get(OUTCOME_SUCCESS, 0),
        "tool_error": counts.get(OUTCOME_TOOL_ERROR, 0),
        "handoff": counts.get(OUTCOME_HANDOFF, 0),
        "self_service_rate": _rate(counts.get(OUTCOME_SUCCESS, 0), total),
        "tool_error_rate": _rate(counts.get(OUTCOME_TOOL_ERROR, 0), total),
        "escalation_rate": _rate(counts.get(OUTCOME_HANDOFF, 0), total),
        "by_outcome": counts,
    }


def _session_level(db, sources: Optional[set], window_days: Optional[int]) -> dict:
    """会话级指标:**与轮级是两个不同的口径,不能互相换算。**

    一个会话里 20 轮只有 1 轮转人工,轮级转人工率 5%、会话级 100% —— 两个数都对,
    回答的是不同问题。运营关心的"自助解决率"是**会话级**的:这通会话到底有没有
    最终落到人工手上。

    `containment_rate`(自助解决率/收容率)= 没有任何一轮转人工的会话 / 总会话。
    """
    sql = ("SELECT session_id, SUM(CASE WHEN outcome = ? THEN 1 ELSE 0 END) AS hs, "
           "COUNT(*) AS turns FROM skill_traces WHERE 1=1")
    params: list = [OUTCOME_HANDOFF]
    if sources:
        sql += " AND COALESCE(source, 'unknown') IN (%s)" % ",".join("?" * len(sources))
        params += sorted(sources)
    if window_days:
        from app.db import dialect
        sql += f" AND created_at >= {dialect.now_minus(int(window_days), 'days')}"
    sql += " GROUP BY session_id"

    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()

    total = len(rows)
    escalated = sum(1 for r in rows if int(r["hs"] or 0) > 0)
    turns = sum(int(r["turns"] or 0) for r in rows)
    return {
        "sessions": total,
        "sessions_escalated": escalated,
        "containment_rate": _rate(total - escalated, total),
        "session_escalation_rate": _rate(escalated, total),
        "avg_turns_per_session": (turns / total) if total else None,
    }


def _satisfaction(db, window_days: Optional[int]) -> dict:
    """满意度代理:差评率。

    **`reviews` 表没有 source 列**,所以这一项无法按流量来源过滤 —— 如实标注这个
    边界,而不是假装它也过滤了。评价是买家在订单侧留下的,与客服会话的流量来源不是
    同一个维度,补一个 source 列也解决不了归属问题(一条差评未必与客服有关)。
    """
    sql = "SELECT rating FROM reviews WHERE 1=1"
    if window_days:
        from app.db import dialect
        sql += f" AND created_at >= {dialect.now_minus(int(window_days), 'days')}"
    conn = db.connect()
    try:
        ratings = [int(dict(r)["rating"]) for r in conn.execute(sql).fetchall()
                   if dict(r).get("rating") is not None]
    finally:
        conn.close()
    bad = sum(1 for x in ratings if x <= BAD_RATING_MAX)
    return {
        "reviews": len(ratings),
        "bad_reviews": bad,
        "bad_review_rate": _rate(bad, len(ratings)),
        "source_filtered": False,      # 如实标注:这一项没有按来源过滤
    }


def _distortion(live: dict, allsrc: dict, keys: tuple[str, ...]) -> dict:
    """同一指标在"只认真实流量"与"全量"两种口径下的差距。

    这是这份报表的**主张所在**:不过滤会得出什么结论,过滤后又是什么结论。
    实测形态是双向的 —— `track-order` 的成功率是全量**偏低**(压测失败拉低),
    而工具失败率则是全量**偏高**。所以不能只说"全量偏乐观"或"全量偏悲观",
    要把两个数并排放着。
    """
    out = {}
    for k in keys:
        a, b = live.get(k), allsrc.get(k)
        if a is None or b is None:
            out[k] = {"live": a, "all_sources": b, "delta": None}
            continue
        out[k] = {"live": round(a, 4), "all_sources": round(b, 4),
                  "delta": round(a - b, 4)}
    return out


def business_report(window_days: Optional[int] = None,
                    min_samples: Optional[int] = None,
                    db=None) -> dict:
    """真实流量上的业务效果报表。

    `window_days=None` 表示全时段。`min_samples` 缺省取
    `settings.anomaly_min_samples`(5)—— 与异常扫描共用同一个"样本够不够"的门槛,
    不在这里另定一个数。
    """
    from app.agent.runtime_context import DECISION_SOURCES
    from app.config.settings import settings

    if db is None:
        from app.db import get_db
        db = get_db()
    floor = int(min_samples if min_samples is not None
                else getattr(settings, "anomaly_min_samples", 5))

    turn_live = _turn_level(db, set(DECISION_SOURCES), window_days)
    turn_all = _turn_level(db, None, window_days)
    sess_live = _session_level(db, set(DECISION_SOURCES), window_days)
    sess_all = _session_level(db, None, window_days)
    sat = _satisfaction(db, window_days)

    # 样本不足的指标值置 None 并登记原因。**不是过滤掉这一项** —— 报表里少一行
    # 与"这一行算不出来"在读者看来完全不同,前者会被当成"这个维度不存在"。
    insufficient: list[dict] = []

    def _guard(block: dict, n_key: str, metric_keys: tuple[str, ...], scope: str):
        n = int(block.get(n_key) or 0)
        if n >= floor:
            return
        for k in metric_keys:
            block[k] = None
        insufficient.append({"scope": scope, "samples": n, "min_samples": floor,
                             "metrics": list(metric_keys)})

    _guard(turn_live, "turns",
           ("self_service_rate", "tool_error_rate", "escalation_rate"), "轮级(live)")
    _guard(sess_live, "sessions",
           ("containment_rate", "session_escalation_rate", "avg_turns_per_session"),
           "会话级(live)")
    _guard(sat, "reviews", ("bad_review_rate",), "满意度(差评率)")

    return {
        "success": True,
        "window_days": window_days,
        "decision_sources": sorted(DECISION_SOURCES),
        "turn_level": turn_live,
        "session_level": sess_live,
        "satisfaction": sat,
        # 全量对照:证明"按来源过滤"不是一句方法论,而是会改变结论的一步
        "all_sources": {"turn_level": turn_all, "session_level": sess_all},
        "distortion": {
            "turn_level": _distortion(turn_live, turn_all,
                                      ("self_service_rate", "tool_error_rate",
                                       "escalation_rate")),
            "session_level": _distortion(sess_live, sess_all,
                                         ("containment_rate", "avg_turns_per_session")),
        },
        "insufficient": insufficient,
        # 本地不做 token 计费(无 token/cost 表),单会话成本只能从 Langfuse 侧取。
        # 明确标 unavailable 而不是给一个估算值:估出来的成本会被当成实测报出去。
        "cost": {"available": False,
                 "reason": "本地无 token 计费表;单会话成本需从 Langfuse 侧统计"},
    }
