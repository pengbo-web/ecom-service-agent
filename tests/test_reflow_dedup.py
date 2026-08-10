"""回流用例必须按用户输入去重。

实测:点一次「回流一次」产出 25 条候选,其中同一句「我买的洗衣机坏了想退货,
运费需要我自己出吗」重复了 5 条以上——因为线上确实被反复问了很多次。

一个回归集里放 5 条一模一样的用例:每次评估为它们各付一次 token、一个偶发
行为按 5 倍权重扭曲通过率,而覆盖面一点没增加。
"""

from __future__ import annotations

from app.evaluation.trace_to_case import collect_reflow_cases


class _Store:
    """按 trace_id 返回预置 trace 的最小 store。"""

    def __init__(self, traces):
        self._traces = traces

    def recent_traces(self, limit=200):
        return [{"trace_id": t["trace_id"]} for t in self._traces][:limit]

    def get_trace(self, tid):
        return next((t for t in self._traces if t["trace_id"] == tid), None)


def _trace(tid, text, *, tools=(), hitl=True, intent="return_request"):
    spans = [{"kind": "tool", "name": f"tool:{n}"} for n in tools]
    if hitl:
        spans.append({"kind": "hitl", "name": "hitl"})
    return {"trace_id": tid, "session_id": f"s-{tid}", "user_input": text,
            "intent": intent, "status": "ok", "spans": spans}


def test_identical_inputs_collapse_to_one_case():
    store = _Store([_trace(f"t{i}", "我买的洗衣机坏了想退货，运费需要我自己出吗")
                    for i in range(5)])
    cases = collect_reflow_cases(store)
    assert len(cases) == 1


def test_different_inputs_are_kept():
    store = _Store([_trace("t1", "退货运费谁出"), _trace("t2", "物流到哪了")])
    assert len(collect_reflow_cases(store)) == 2


def test_whitespace_only_differences_still_dedupe():
    """「退货  运费」和「退货 运费」是同一个用例,不该占两个位。"""
    store = _Store([_trace("t1", "退货 运费谁出"), _trace("t2", "退货   运费谁出")])
    assert len(collect_reflow_cases(store)) == 1


def test_keeps_the_richest_case_among_duplicates():
    """同一句话的多条 trace 里保留断言最强的那条。

    先到先得会让一条什么都没断言的空壳挤掉后面那条真正有价值的——回归集的
    价值全在断言上,留错一条等于白留。
    """
    store = _Store([
        _trace("t1", "退货运费谁出", tools=(), hitl=False, intent=None),
        _trace("t2", "退货运费谁出", tools=("query_order", "apply_refund"), hitl=True),
    ])
    cases = collect_reflow_cases(store)
    assert len(cases) == 1
    assert cases[0]["expected_tools"] == ["query_order", "apply_refund"]
    assert cases[0]["expected_requires_human"] is True


def test_empty_input_is_not_collapsed():
    """空输入无从去重,原样保留——不能让它们互相吞掉。"""
    store = _Store([_trace("t1", ""), _trace("t2", "")])
    assert len(collect_reflow_cases(store)) == 2
