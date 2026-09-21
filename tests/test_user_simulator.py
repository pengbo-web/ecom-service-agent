"""WS5 多轮用户模拟器:停止条件全确定性 + 口径标记 + persona 不 mutate。"""

from app.agent.runtime_context import SOURCE_SIMULATED, get_traffic_source
from app.evaluation.user_simulator import (
    STOP_GAVE_UP,
    STOP_GOAL_MET,
    STOP_MAX_TURNS,
    STOP_SCRIPT_EXHAUSTED,
    Persona,
    load_personas,
    run_in_sandbox,
    run_persona,
)


def _echo_agent(script):
    """按轮次返回预定回复的假 agent。"""
    state = {"i": 0}

    def chat_fn(text):
        reply = script[min(state["i"], len(script) - 1)]
        state["i"] += 1
        return reply

    return chat_fn


def test_goal_met_stops_immediately():
    p = Persona(id="p", opener="运费谁出?", followups=["追问一", "追问二"],
                goal_keywords=["由您承担"], patience=3)
    r = run_persona(p, _echo_agent(["七天无理由退货运费由您承担(约12元起)"]))
    assert r.stop_reason == STOP_GOAL_MET
    assert r.n_turns == 1


def test_script_exhausted():
    p = Persona(id="p", opener="在吗", followups=["然后呢"], goal_keywords=[], patience=5)
    r = run_persona(p, _echo_agent(["好的", "好的"]))
    assert r.stop_reason == STOP_SCRIPT_EXHAUSTED
    assert r.n_turns == 2


def test_gave_up_when_patience_exceeded():
    p = Persona(id="p", opener="在吗", followups=["追一", "追二", "追三"],
                goal_keywords=["不可能出现"], patience=2)
    r = run_persona(p, _echo_agent(["答非所问"]))
    assert r.stop_reason == STOP_GAVE_UP
    assert r.n_turns == 2          # 两轮未满足即放弃,不耗完脚本


def test_max_turns_caps():
    p = Persona(id="p", opener="在吗", followups=["追一", "追二", "追三", "追四"],
                goal_keywords=[], patience=99, max_turns=3)
    r = run_persona(p, _echo_agent(["好"]))
    assert r.stop_reason == STOP_MAX_TURNS
    assert r.n_turns == 3


def test_persona_not_mutated_across_runs():
    p = Persona(id="p", opener="在吗", followups=["追一"], goal_keywords=[], patience=5)
    r1 = run_persona(p, _echo_agent(["好", "好"]))
    r2 = run_persona(p, _echo_agent(["好", "好"]))
    assert p.followups == ["追一"]
    assert (r1.stop_reason, r1.n_turns) == (r2.stop_reason, r2.n_turns)


def test_personas_file_loads():
    personas = load_personas()
    assert len(personas) >= 3
    ids = {p.id for p in personas}
    assert {"policy_pushback", "anaphora_order", "repeat_then_giveup"} <= ids


def test_run_in_sandbox_marks_traffic_simulated(monkeypatch):
    """隔离跑期间流量标记必须是 simulated——两套口径都排除它的前提。"""
    seen = {}

    class _FakeAgent:
        def chat(self, text):
            seen["source"] = get_traffic_source()
            return type("R", (), {"reply": "好的"})()

    class _FakeSandbox:
        def session_path_for(self, case_id):
            return "/tmp/x.json"

        def _build_agent(self, path, patches):
            return _FakeAgent()

    p = Persona(id="p", opener="在吗", followups=[], goal_keywords=[], patience=1)
    run_in_sandbox(p, sandbox=_FakeSandbox())
    assert seen["source"] == SOURCE_SIMULATED
    assert get_traffic_source() != SOURCE_SIMULATED   # 跑完还回去
