from app.agent.history_utils import (
    estimate_tokens, truncate_tool_result, sanitize_tool_pairs, legal_message_start,
)


def _assistant(calls):
    return {"role": "assistant", "content": None, "tool_calls": calls}


def _call(cid, name):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _tool(cid, content="ok"):
    return {"role": "tool", "tool_call_id": cid, "content": content}


# ---------- estimate_tokens ----------
def test_estimate_tokens_grows_with_content():
    short = [{"role": "user", "content": "hi"}]
    long = [{"role": "user", "content": "x" * 1000}]
    assert estimate_tokens(long) > estimate_tokens(short) > 0


# ---------- truncate_tool_result ----------
def test_truncate_over_limit():
    r = truncate_tool_result("y" * 100, 30)
    assert len(r) <= 30 + 40 and "截断" in r


def test_truncate_under_limit_untouched():
    assert truncate_tool_result("short", 100) == "short"


# ---------- sanitize_tool_pairs ----------
def test_drop_orphan_tool_result():
    # tool 结果没有前置 assistant 声明 → 丢弃
    msgs = [{"role": "user", "content": "q"}, _tool("nope")]
    out = sanitize_tool_pairs(msgs)
    assert all(m.get("role") != "tool" for m in out)


def test_backfill_missing_tool_result():
    # assistant 声明了 tool_call 但没有对应 tool 结果 → 回填
    msgs = [{"role": "user", "content": "q"}, _assistant([_call("c1", "query_order")])]
    out = sanitize_tool_pairs(msgs)
    tools = [m for m in out if m.get("role") == "tool"]
    assert len(tools) == 1 and tools[0]["tool_call_id"] == "c1"


def test_strip_malformed_tool_call():
    # tool_call 无有效 name → 剥离;assistant 无内容无有效调用 → 整条丢弃
    bad = {"id": "c1", "type": "function", "function": {"name": "", "arguments": "{}"}}
    msgs = [{"role": "user", "content": "q"}, _assistant([bad])]
    out = sanitize_tool_pairs(msgs)
    assert all(m.get("role") != "assistant" for m in out)


def test_valid_pair_preserved():
    msgs = [{"role": "user", "content": "q"},
            _assistant([_call("c1", "query_order")]), _tool("c1")]
    out = sanitize_tool_pairs(msgs)
    assert len(out) == 3


# ---------- legal_message_start ----------
def test_legal_start_finds_user():
    msgs = [_tool("x"), {"role": "assistant", "content": "a"}, {"role": "user", "content": "u"}]
    assert legal_message_start(msgs) == 2


def test_legal_start_no_user():
    assert legal_message_start([{"role": "assistant", "content": "a"}]) == 0
