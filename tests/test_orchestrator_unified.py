"""B1:多 Agent 编排器复用硬化引擎(EcomAgent)+ 按路由切画像。monkeypatch 避免联网。"""

from app.agent.chat import EcomAgent
from app.multi_agent.orchestrator import MultiAgentOrchestrator
from app.config.settings import settings


def _orch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    return MultiAgentOrchestrator(session_path=str(tmp_path / "s.json"), user_id="u1")


def _tool_names(tm):
    return {d["function"]["name"] for d in tm.tool_definitions}


def test_engine_is_hardened_ecomagent(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    assert isinstance(o.engine, EcomAgent)          # 复用同一硬化引擎
    assert set(o.profiles) == {"presale", "postsale", "complaint"}


def test_delegates_state_to_engine(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    assert o.raw_messages is o.engine.raw_messages
    assert o.memory_manager is o.engine.memory_manager
    assert o.session_id == o.engine.session_id
    assert o.user_id == "u1"


def test_profiles_expose_only_allowed_tools(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    presale = _tool_names(o.profiles["presale"]["tool_manager"])
    postsale = _tool_names(o.profiles["postsale"]["tool_manager"])
    assert "apply_refund" in postsale          # 售后能退款
    assert "apply_refund" not in presale       # 售前不能退款(工具隔离)
    assert "query_product" in presale


def test_chat_switches_profile_and_delegates(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    events = []
    o.event_sink = lambda e: events.append(e)

    monkeypatch.setattr(o.router, "route", lambda *a, **k: "postsale")
    captured = {}

    def fake_chat(user_input):
        captured["system_prompt"] = o.engine.system_prompt
        captured["tools"] = _tool_names(o.engine.tool_manager)
        captured["event_sink"] = o.engine.event_sink
        return "ok"

    monkeypatch.setattr(o.engine, "chat", fake_chat)
    out = o.chat("我要退款")

    assert out == "ok"
    # 切到了售后画像:prompt + 工具子集都换成 postsale 的
    assert o.profiles["postsale"]["prompt"] == captured["system_prompt"]
    assert "apply_refund" in captured["tools"]
    # event_sink 透传给了引擎;并发了 route 事件
    assert captured["event_sink"] is not None
    assert any(e.get("type") == "route" and e.get("key") == "postsale" for e in events)
