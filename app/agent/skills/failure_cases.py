"""G3 失败案例采集:按真实执行轨迹把失败会话归到具体 skill 名下。

与旧做法的区别:`app/scripts/synthesize_skills.py::related_failures` 靠"首条用户
消息关键词 ∩ skill description 关键词"猜关联;本模块直接读 `skill_traces`——
"这一轮确实加载了该 skill,且结局是转人工/工具失败"是事实,不是猜测。

产出的样本形状与 `session_archive` 一致(messages list / summary),可直接喂
`synthesizer.improve_skill`。
"""

from __future__ import annotations

from app.agent.skills.execution_trace import OUTCOME_HANDOFF, OUTCOME_TOOL_ERROR

# 视为"该 skill 没搞定"的结局:转人工 / 工具报错
FAILURE_OUTCOMES = [OUTCOME_HANDOFF, OUTCOME_TOOL_ERROR]


def collect_failures_by_skill(
    traces: list[dict], archives: list[dict], max_per_skill: int = 5,
) -> dict[str, list[dict]]:
    """把失败轨迹关联到归档会话,按 skill 分组返回可用于改进的样本。

    - 只取 outcome 命中 FAILURE_OUTCOMES 的轨迹;
    - 按 session_id 关联归档会话,关联不到的轨迹跳过(会话尚未归档);
    - 同一会话在同一 skill 下只算一个样本(去重,避免重复内容喂 LLM);
    - 每个 skill 最多 max_per_skill 条(控 prompt 体积与成本)。
    """
    by_session = {a.get("session_id"): a for a in archives if a.get("session_id")}

    grouped: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()

    for trace in traces:
        if trace.get("outcome") not in FAILURE_OUTCOMES:
            continue
        skill_name = str(trace.get("skill_name") or "").strip()
        session_id = trace.get("session_id")
        if not skill_name or not session_id:
            continue

        archive = by_session.get(session_id)
        if archive is None:
            continue

        key = (skill_name, session_id)
        if key in seen:
            continue

        bucket = grouped.setdefault(skill_name, [])
        if len(bucket) >= max_per_skill:
            continue

        seen.add(key)
        bucket.append(archive)

    return grouped
