"""H1.1:回复流水线三提示词（评估器 / 重写 / 润色）的字符串常量测试。

H1.2 新增:ReplyPipeline + FunctionalSelector（LLM ReAct 选择器动态调度,规则兜底）的单测。
全部用脚本化 fake client（不触网），驱动 pipeline.run(...)。
"""

import json

from app.prompts.reply_pipeline import (
    EVALUATOR_PROMPT,
    REDRAFT_PROMPT,
    POLISH_PROMPT,
)
from app.agent.reply_pipeline import ReplyPipeline, FunctionalSelector
from app.config.settings import settings


def test_evaluator_prompt_nonempty_and_grounded():
    assert EVALUATOR_PROMPT.strip()
    assert "接地" in EVALUATOR_PROMPT


def test_redraft_prompt_nonempty_and_fact_based():
    assert REDRAFT_PROMPT.strip()
    assert "事实" in REDRAFT_PROMPT


def test_polish_prompt_nonempty_and_fact_locked():
    assert POLISH_PROMPT.strip()
    assert "不得改变任何事实" in POLISH_PROMPT


# ============================================================
# H1.2：ReplyPipeline + FunctionalSelector
# ============================================================

class _FakeCompletions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls.append(kwargs)
        content = self.outer.script.pop(0)
        msg = type("M", (), {"content": content})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


class FakeClient:
    """脚本化 fake client：chat.completions.create(...) 按调用次序依次吐出预设内容。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.chat = type("Chat", (), {"completions": _FakeCompletions(self)})()


def _select_events(events):
    return [e for e in events if e.get("type") == "select"]


def _evaluate_events(events):
    return [e for e in events if e.get("type") == "evaluate"]


def _collect_emit():
    events = []
    return events, lambda ev: events.append(ev)


# ---- 1. 简单轮：complex_turn=False → 原样返回 draft,零 LLM 调用 ----
def test_simple_turn_returns_draft_with_zero_llm_calls(monkeypatch):
    monkeypatch.setattr(settings, "reply_pipeline_enabled", True)
    client = FakeClient([])
    events, emit = _collect_emit()
    out = ReplyPipeline().run(
        client=client, model="test", user_input="我的订单到哪了",
        draft="您的订单正在路上", grounding="", complex_turn=False, emit=emit,
    )
    assert out == "您的订单正在路上"
    assert client.calls == []
    assert events == []


# ---- 2. LLM 选择器正常路径 ----
def test_llm_selector_happy_path(monkeypatch):
    monkeypatch.setattr(settings, "reply_pipeline_enabled", True)
    monkeypatch.setattr(settings, "selector_mode", "llm")
    monkeypatch.setattr(settings, "reply_pipeline_max_rounds", 2)
    script = [
        json.dumps({"next": "evaluate", "reason": "先检查草稿"}),
        json.dumps({"ok": True, "issues": [], "suggestion": ""}),
        json.dumps({"next": "polish", "reason": "评估通过,润色语气"}),
        "亲，您的订单正在飞速赶来的路上哦～",
        json.dumps({"next": "done", "reason": "已润色,结束"}),
    ]
    client = FakeClient(script)
    events, emit = _collect_emit()
    out = ReplyPipeline().run(
        client=client, model="test", user_input="我的订单到哪了",
        draft="您的订单正在路上", grounding="物流:运输中", complex_turn=True, emit=emit,
    )
    assert out == "亲，您的订单正在飞速赶来的路上哦～"
    selects = _select_events(events)
    assert [e["next"] for e in selects] == ["evaluate", "polish", "done"]
    assert all(e.get("reason") for e in selects)
    assert len(_evaluate_events(events)) == 1
    assert _evaluate_events(events)[0]["ok"] is True


# ---- 3. 循环 + 重评估：redraft 后又评估一次 ----
def test_redraft_triggers_reevaluation(monkeypatch):
    monkeypatch.setattr(settings, "reply_pipeline_enabled", True)
    monkeypatch.setattr(settings, "selector_mode", "llm")
    monkeypatch.setattr(settings, "reply_pipeline_max_rounds", 3)
    script = [
        json.dumps({"next": "evaluate", "reason": "先查草稿"}),
        json.dumps({"ok": False, "issues": ["脱离工具结果"], "suggestion": "补充物流信息"}),
        json.dumps({"next": "redraft", "reason": "不合格,重写"}),
        "重写后的回复：您的订单已发货，预计明天送达。",
        json.dumps({"next": "evaluate", "reason": "重写后再评估"}),
        json.dumps({"ok": True, "issues": [], "suggestion": ""}),
        json.dumps({"next": "polish", "reason": "评估通过,润色"}),
        "亲，您的订单已经发货啦，预计明天就能到您手上哦～",
        json.dumps({"next": "done", "reason": "结束"}),
    ]
    client = FakeClient(script)
    events, emit = _collect_emit()
    out = ReplyPipeline().run(
        client=client, model="test", user_input="我的订单发货了吗",
        draft="订单已发货", grounding="物流:已发货,预计明天送达", complex_turn=True, emit=emit,
    )
    assert out == "亲，您的订单已经发货啦，预计明天就能到您手上哦～"
    assert len(_evaluate_events(events)) >= 2


# ---- 4. 达 max_rounds 收敛：即使选择器一直想 redraft,也强制 polish→done ----
def test_converges_when_max_rounds_reached(monkeypatch):
    monkeypatch.setattr(settings, "reply_pipeline_enabled", True)
    monkeypatch.setattr(settings, "selector_mode", "llm")
    monkeypatch.setattr(settings, "reply_pipeline_max_rounds", 1)
    script = [
        json.dumps({"next": "evaluate", "reason": "先查草稿"}),
        json.dumps({"ok": False, "issues": ["不合格"], "suggestion": "重写"}),
        json.dumps({"next": "redraft", "reason": "还想重写"}),   # rounds(1)>=max(1) → 强制 polish
        "润色前的最终文本",
        json.dumps({"next": "redraft", "reason": "还想重写"}),   # 已 polish → 强制 done
    ]
    client = FakeClient(script)
    events, emit = _collect_emit()
    out = ReplyPipeline().run(
        client=client, model="test", user_input="问题",
        draft="草稿", grounding="工具结果", complex_turn=True, emit=emit,
    )
    assert out == "润色前的最终文本"
    selects = _select_events(events)
    assert selects[-2]["next"] == "polish"
    assert selects[-1]["next"] == "done"


# ---- 5. 规则兜底：selector_mode="rule" 全程走规则；LLM 输出非法 JSON 自动回退规则 ----
def test_rule_selector_end_to_end(monkeypatch):
    monkeypatch.setattr(settings, "reply_pipeline_enabled", True)
    monkeypatch.setattr(settings, "selector_mode", "rule")
    monkeypatch.setattr(settings, "reply_pipeline_max_rounds", 2)
    script = [
        json.dumps({"ok": False, "issues": ["脱离结果"], "suggestion": "重写"}),
        "重写文本",
        json.dumps({"ok": True, "issues": [], "suggestion": ""}),
        "润色文本",
    ]
    client = FakeClient(script)
    events, emit = _collect_emit()
    out = ReplyPipeline().run(
        client=client, model="test", user_input="问题",
        draft="草稿", grounding="工具结果", complex_turn=True, emit=emit,
    )
    assert out == "润色文本"
    assert len(client.calls) == 4   # 规则模式选择器零 LLM 调用


def test_llm_selector_invalid_json_falls_back_to_rule(monkeypatch):
    monkeypatch.setattr(settings, "reply_pipeline_enabled", True)
    monkeypatch.setattr(settings, "selector_mode", "llm")
    monkeypatch.setattr(settings, "reply_pipeline_max_rounds", 2)
    script = [
        json.dumps({"next": "garbage"}),                          # 非法 → 回退规则 → evaluate
        json.dumps({"ok": True, "issues": [], "suggestion": ""}),
        json.dumps({"next": "garbage"}),                          # 非法 → 回退规则 → polish
        "润色文本",
        json.dumps({"next": "garbage"}),                          # 非法 → 回退规则 → done
    ]
    client = FakeClient(script)
    events, emit = _collect_emit()
    out = ReplyPipeline().run(
        client=client, model="test", user_input="问题",
        draft="草稿", grounding="工具结果", complex_turn=True, emit=emit,
    )
    assert out == "润色文本"


# ---- 5c. 真 LLM 复现：选择器润色后仍反复返回 polish,必须一次润色即收敛 done ----
def test_llm_selector_repeated_polish_still_converges(monkeypatch):
    monkeypatch.setattr(settings, "reply_pipeline_enabled", True)
    monkeypatch.setattr(settings, "selector_mode", "llm")
    monkeypatch.setattr(settings, "reply_pipeline_max_rounds", 2)
    script = [
        json.dumps({"next": "evaluate", "reason": "先查"}),
        json.dumps({"ok": True, "issues": [], "suggestion": ""}),
        json.dumps({"next": "polish", "reason": "润色"}),
        "润色文本",
        # 之后即便选择器还想 polish,也不该再被执行(短路到 done)
        json.dumps({"next": "polish", "reason": "又想润色"}),
        json.dumps({"next": "polish", "reason": "还想润色"}),
    ]
    client = FakeClient(script)
    events, emit = _collect_emit()
    out = ReplyPipeline().run(
        client=client, model="test", user_input="问题",
        draft="草稿", grounding="工具结果", complex_turn=True, emit=emit,
    )
    assert out == "润色文本"
    assert len([e for e in events if e.get("type") == "polish"]) == 1   # 只润色一次
    assert _select_events(events)[-1]["next"] == "done"


# ---- 6. fail-open：评估器坏 JSON 不阻断；总开关关闭原样返回 draft ----
def test_evaluator_bad_json_fails_open(monkeypatch):
    monkeypatch.setattr(settings, "reply_pipeline_enabled", True)
    monkeypatch.setattr(settings, "selector_mode", "rule")
    monkeypatch.setattr(settings, "reply_pipeline_max_rounds", 2)
    script = [
        "这不是合法JSON也没有ok字段",   # 评估器坏输出 → fail-open 视为 ok=True
        "最终润色文本",
    ]
    client = FakeClient(script)
    events, emit = _collect_emit()
    out = ReplyPipeline().run(
        client=client, model="test", user_input="问题",
        draft="草稿", grounding="工具结果", complex_turn=True, emit=emit,
    )
    assert out
    assert out == "最终润色文本"
    assert _evaluate_events(events)[0]["ok"] is True


def test_pipeline_disabled_returns_draft(monkeypatch):
    monkeypatch.setattr(settings, "reply_pipeline_enabled", False)
    client = FakeClient([])
    out = ReplyPipeline().run(
        client=client, model="test", user_input="问题",
        draft="原样草稿", grounding="", complex_turn=True, emit=None,
    )
    assert out == "原样草稿"
    assert client.calls == []


# ---- FunctionalSelector 单独测试 choose_rule 的边界 ----
def test_choose_rule_converges():
    selector = FunctionalSelector()
    state = {"draft": "d", "verdict": None, "polished": None, "rounds": 0, "max_rounds": 2}
    assert selector.choose_rule(state) == "evaluate"

    state["verdict"] = {"ok": False, "issues": ["x"], "suggestion": "y"}
    state["rounds"] = 1
    assert selector.choose_rule(state) == "redraft"

    state["rounds"] = 2   # 已达 max_rounds,即使 not ok 也不再 redraft
    assert selector.choose_rule(state) == "polish"

    state["verdict"] = {"ok": True, "issues": [], "suggestion": ""}
    assert selector.choose_rule(state) == "polish"

    state["polished"] = "已润色"
    assert selector.choose_rule(state) == "done"
