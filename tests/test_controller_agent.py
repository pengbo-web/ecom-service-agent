"""H1.0-B:总控 Agent(MultiAgentOrchestrator)显式化四项只读职责入口。

只测结构/属性/工厂类型,不调用 .chat()(会触发真 LLM)。用临时 memory_dir 构造。
"""

from app.agent.chat import EcomAgent
from app.agent.consent import RISK_ACTIONS
from app.multi_agent.orchestrator import MultiAgentOrchestrator
from app.config.settings import settings


def _orch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    return MultiAgentOrchestrator(session_path=str(tmp_path / "s.json"), user_id="u1")


def test_react_entry_is_engine(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    assert o.react is o.engine
    assert isinstance(o.react, EcomAgent)
    assert hasattr(o.react, "_react_loop")


def test_memory_entry_is_memory_manager(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    assert o.memory is o.engine.memory_manager


def test_permissions_entry(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    perms = o.permissions
    assert perms["risk_actions"] is RISK_ACTIONS
    assert perms["consent"] is True
    assert perms["idempotency"] is True
    assert perms["escalation"] is True


def test_lifecycle_entry(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    lc = o.lifecycle
    # 三个生命周期动作可调用
    assert callable(lc["save"])
    assert callable(lc["close"])
    assert callable(lc["reset"])
    # 当前引擎状态透传
    assert lc["status"] == o.engine._status
    assert lc["step_seq"] == o.engine._step_seq


def test_capabilities_lists_four(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    caps = o.capabilities()
    assert set(caps) == {"react", "memory", "permissions", "lifecycle"}
