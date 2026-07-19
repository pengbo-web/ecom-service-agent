from app.agent.chat import EcomAgent


def _make_agent():
    # 构造不触发任何网络调用（OpenAI() 仅存配置；Memory/Skill 仅读本地文件）
    return EcomAgent(session_path="app/sessions/_test_emit.json")


def test_emit_routes_to_custom_sink():
    agent = _make_agent()
    captured = []
    agent.event_sink = captured.append

    agent._emit({"type": "thought", "content": "我在想"})
    agent._emit({"type": "tool_call", "name": "get_order", "args": {"id": "A1"}})
    agent._emit({"type": "tool_result", "content": "结果"})

    assert captured == [
        {"type": "thought", "content": "我在想"},
        {"type": "tool_call", "name": "get_order", "args": {"id": "A1"}},
        {"type": "tool_result", "content": "结果"},
    ]


def test_emit_default_prints_to_console(capsys):
    agent = _make_agent()  # event_sink 默认为 None
    agent._emit({"type": "thought", "content": "思考内容"})
    agent._emit({"type": "tool_call", "name": "get_order", "args": {"id": "A1"}})
    agent._emit({"type": "tool_result", "content": "x" * 400})

    out = capsys.readouterr().out
    assert "💭 [思考] 思考内容" in out
    assert "🔧 [调用工具] get_order(id='A1')" in out
    assert "📋 [工具结果]" in out
    assert "..." in out  # 超过 300 字被截断
