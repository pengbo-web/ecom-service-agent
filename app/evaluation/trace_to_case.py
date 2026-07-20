"""线上 Trace 回流成评估用例（问题对话沉淀为回归测试）。"""


def is_problem_trace(trace: dict) -> bool:
    if trace.get("status") == "error":
        return True
    if trace.get("intent") == "blocked":
        return True
    if any(s.get("kind") == "hitl" for s in trace.get("spans", [])):
        return True
    return False


def trace_to_case(trace: dict) -> dict:
    tid = str(trace.get("trace_id", ""))[:8]
    user_input = trace.get("user_input", "")
    case = {
        "id": f"reflow-{tid}",
        "description": f"线上回流: {user_input[:20]}",
        "turns": [user_input],
    }
    intent = trace.get("intent")
    if intent and intent not in ("blocked", "unknown"):
        case["expected_intent"] = intent

    tools = [s["name"].split(":", 1)[1]
             for s in trace.get("spans", [])
             if s.get("kind") == "tool" and ":" in s.get("name", "")]
    if tools:
        case["expected_tools"] = tools

    if any(s.get("kind") == "hitl" for s in trace.get("spans", [])):
        case["expected_requires_human"] = True

    return case
