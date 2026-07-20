from app.evaluation.trace_to_case import trace_to_case, is_problem_trace


def _trace(**over):
    t = {"trace_id": "abc12345", "user_input": "我的订单发货了吗", "intent": "order_query",
         "status": "ok", "spans": []}
    t.update(over)
    return t


def test_basic_case_fields():
    c = trace_to_case(_trace())
    assert c["turns"] == ["我的订单发货了吗"]
    assert "abc12345" in c["id"]
    assert c["expected_intent"] == "order_query"


def test_tool_spans_become_expected_tools():
    t = _trace(spans=[{"kind": "tool", "name": "tool:query_order"},
                      {"kind": "llm", "name": "llm.chat.create"}])
    c = trace_to_case(t)
    assert c["expected_tools"] == ["query_order"]


def test_hitl_span_sets_requires_human():
    t = _trace(spans=[{"kind": "hitl", "name": "handoff"}])
    assert trace_to_case(t)["expected_requires_human"] is True


def test_blocked_intent_not_used_as_expected():
    c = trace_to_case(_trace(intent="blocked"))
    assert "expected_intent" not in c


def test_is_problem_trace():
    assert is_problem_trace(_trace(status="error")) is True
    assert is_problem_trace(_trace(intent="blocked")) is True
    assert is_problem_trace(_trace(spans=[{"kind": "hitl", "name": "handoff"}])) is True
    assert is_problem_trace(_trace()) is False
