"""WS4 群体标签统计:cohort × 失败率对照表(技术方案 §5,**只报不回流**)。

参考图把"用户建模"画在 Skill 生成盒内(偏好捕捉服务于 skill 生成);本项目
守住挂载点分歧:个体偏好标签进 `UserProfile.tags` 影响**对该用户**的应答,
**不进全局 skill**。本模块给出群体视角的只读报表——哪类标签人群的失败/转人工
集中——是否据此改 skill 由人决策、走既有闭环(归因→门禁→五道闸)。

判据全部确定性计数,零 LLM;样本不足(`min_samples`,默认取
`settings.anomaly_min_samples`=5)的 cohort 率值置 **None 而不是 0.0**——
"1 单退 1 单是 100% 退款率,但那不是异常,是没数据"(anomaly.py 同一条纪律)。
"""

from __future__ import annotations


def _user_outcome_rates(db) -> dict[str, dict[str, int]]:
    """`{user_id: {"total": n, "handoff": n, "tool_error": n}}`,全口径轨迹。

    报表用全口径是刻意的:cohort 对照看的是"哪类人群的会话长得不一样",
    不是看门狗判定;判定口径(DECISION_SOURCES)在 watchdog 那一侧,两处不混用。
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT user_id, outcome, COUNT(*) AS n FROM skill_traces "
            "WHERE user_id IS NOT NULL GROUP BY user_id, outcome").fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        item = dict(row)
        bucket = out.setdefault(str(item["user_id"]),
                                {"total": 0, "handoff": 0, "tool_error": 0})
        n = int(item["n"])
        bucket["total"] += n
        outcome = str(item["outcome"])
        if outcome == "handoff":
            bucket["handoff"] += n
        elif outcome == "tool_error":
            bucket["tool_error"] += n
    return out


def _rate(part: int, total: int, min_samples: int):
    if total < min_samples:
        return None          # 没数据 ≠ 全成功
    return round(part / total, 3) if total else None


def cohort_report(db=None, profile_store=None, min_samples: int | None = None
                  ) -> list[dict]:
    """每个标签一行:{tag, users, traces, handoff_rate, tool_error_rate}。

    按 traces 降序(信息量大的在前)。率值可能为 None(样本不足)——渲染方必须
    把 None 显示成"n/a"而不是 0。
    """
    if db is None:
        from app.db import get_db
        db = get_db()
    if profile_store is None:
        from app.agent.memory.profile import get_profile_store
        profile_store = get_profile_store()
    if min_samples is None:
        from app.config.settings import settings
        min_samples = int(settings.anomaly_min_samples)
    if profile_store is None:
        return []

    user_tags = profile_store.list_user_tags()
    if not user_tags:
        return []
    rates = _user_outcome_rates(db)

    by_tag: dict[str, dict[str, int]] = {}
    for user_id, tags in user_tags.items():
        bucket_src = rates.get(user_id, {"total": 0, "handoff": 0, "tool_error": 0})
        for tag in tags:
            agg = by_tag.setdefault(tag, {"users": 0, "total": 0,
                                          "handoff": 0, "tool_error": 0})
            agg["users"] += 1
            agg["total"] += bucket_src["total"]
            agg["handoff"] += bucket_src["handoff"]
            agg["tool_error"] += bucket_src["tool_error"]

    rows = []
    for tag, agg in by_tag.items():
        rows.append({
            "tag": tag,
            "users": agg["users"],
            "traces": agg["total"],
            "handoff_rate": _rate(agg["handoff"], agg["total"], min_samples),
            "tool_error_rate": _rate(agg["tool_error"], agg["total"], min_samples),
        })
    rows.sort(key=lambda r: (-r["traces"], r["tag"]))
    return rows
