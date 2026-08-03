"""G3 失败案例采集:用真实执行轨迹(skill_traces)关联失败会话,替代关键词猜测。"""

from app.agent.skills.failure_cases import FAILURE_OUTCOMES, collect_failures_by_skill


def _trace(session_id, skill_name, outcome):
    return {"session_id": session_id, "skill_name": skill_name,
            "outcome": outcome, "tool_calls": []}


def _archive(session_id, text, summary=""):
    return {"session_id": session_id, "user_id": "u1", "summary": summary,
            "messages": [{"role": "user", "content": text},
                         {"role": "assistant", "content": "处理中"}]}


def test_failure_outcomes_are_handoff_and_tool_error():
    assert set(FAILURE_OUTCOMES) == {"handoff", "tool_error"}


def test_collects_only_failed_traces_grouped_by_skill():
    traces = [
        _trace("s1", "process-return", "handoff"),
        _trace("s2", "process-return", "success"),      # 成功轮不采
        _trace("s3", "track-order", "tool_error"),
    ]
    archives = [_archive("s1", "退款一直没人处理"),
                _archive("s2", "怎么退货"),
                _archive("s3", "快递到哪了")]

    grouped = collect_failures_by_skill(traces, archives)

    assert set(grouped) == {"process-return", "track-order"}
    assert [m["content"] for m in grouped["process-return"][0]["messages"]][0] == "退款一直没人处理"
    assert len(grouped["track-order"]) == 1


def test_trace_without_matching_archive_is_skipped():
    traces = [_trace("missing", "process-return", "handoff")]
    assert collect_failures_by_skill(traces, []) == {}


def test_duplicate_sessions_deduped():
    """同一会话多轮失败只算一个样本,避免同内容重复喂 LLM。"""
    traces = [_trace("s1", "process-return", "handoff"),
              _trace("s1", "process-return", "tool_error")]
    archives = [_archive("s1", "退款没人管")]

    grouped = collect_failures_by_skill(traces, archives)
    assert len(grouped["process-return"]) == 1


def test_respects_max_per_skill():
    traces = [_trace(f"s{i}", "process-return", "handoff") for i in range(5)]
    archives = [_archive(f"s{i}", f"问题{i}") for i in range(5)]

    grouped = collect_failures_by_skill(traces, archives, max_per_skill=2)
    assert len(grouped["process-return"]) == 2


def test_empty_inputs_return_empty():
    assert collect_failures_by_skill([], []) == {}


def test_canary_variant_failures_are_not_blamed_on_live_skill():
    """灰度候选造成的失败不得算作 live skill 的失败。"""
    traces = [{"session_id": "s1", "skill_name": "process-return",
               "outcome": "handoff", "variant": "canary", "tool_calls": []}]
    archives = [_archive("s1", "退款没人管")]
    assert collect_failures_by_skill(traces, archives) == {}
