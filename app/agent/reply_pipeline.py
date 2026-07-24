"""H1.2：回复流水线（出话/评估/重写/润色）+ LLM ReAct 选择器动态调度（G1）。

对齐总控 ReAct 编排「出话/评估/润色」的架构语义：每一步都真用 LLM 推理决定下一个
功能 Agent（selector），可循环，直到收敛为 ``done``。``selector_mode`` 默认 ``"llm"``；
LLM 选择器异常/关闭/输出非法时，退回规则选择器（``choose_rule``）保稳。

分级门控：简单轮（``complex_turn=False``）或总开关关闭（``settings.reply_pipeline_enabled``）
时，原样返回草稿，不产生任何 LLM 调用成本。

fail-open 铁律：评估器坏 JSON / 异常 → 视为 ``ok=True``，绝不抛出、绝不阻断；
重写/润色 LLM 异常 → 回退保留上一版文本，流程继续。
"""

import json
from typing import Callable, Optional

from app.config.settings import settings
from app.prompts.reply_pipeline import (
    EVALUATOR_PROMPT,
    REDRAFT_PROMPT,
    POLISH_PROMPT,
    SELECTOR_PROMPT,
)

_VALID_NEXT = {"evaluate", "redraft", "polish", "done"}


def _parse_json(raw: str) -> dict:
    """解析 LLM 返回的 JSON（剥离可能的 ```代码块```），非 dict / 解析失败均抛异常。"""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data


def _emit(emit: Optional[Callable[[dict], None]], event: dict) -> None:
    if emit is not None:
        emit(event)


class FunctionalSelector:
    """决定下一步走 evaluate / redraft / polish / done 的选择器：LLM 推理，规则兜底。"""

    def choose_llm(self, client, model: str, state: dict) -> str:
        """用 SELECTOR_PROMPT + 当前状态问一次 LLM，解析出 next。非法输出直接抛异常。"""
        verdict = state.get("verdict")
        messages = [
            {"role": "system", "content": SELECTOR_PROMPT},
            {
                "role": "user",
                "content": (
                    f"用户问题：{state.get('user_input', '')}\n\n"
                    f"当前草稿：{state.get('draft', '')}\n\n"
                    f"最近一次评估结论：{verdict if verdict is not None else '（尚未评估）'}\n\n"
                    f"已进行轮次：{state.get('rounds', 0)}"
                ),
            },
        ]
        resp = client.chat.completions.create(model=model, messages=messages, temperature=0.0)
        content = resp.choices[0].message.content or ""
        data = _parse_json(content)
        next_role = data.get("next")
        if next_role not in _VALID_NEXT:
            raise ValueError(f"selector 输出非法 next: {next_role!r}")
        return next_role

    def choose_rule(self, state: dict) -> str:
        """规则兜底：未评估过→evaluate；不合格且未达上限→redraft；否则→polish；已润色→done。"""
        if state.get("polished") is not None:
            return "done"
        verdict = state.get("verdict")
        if verdict is None:
            return "evaluate"
        ok = bool(verdict.get("ok", True))
        rounds = state.get("rounds", 0)
        max_rounds = state.get("max_rounds", 0)
        if not ok and rounds < max_rounds:
            return "redraft"
        return "polish"


class ReplyPipeline:
    """回复流水线：出话草稿 → （可能反复）评估/重写 → 润色。见模块 docstring 的整体语义。"""

    def run(
        self,
        client,
        model: str,
        user_input: str,
        draft: str,
        grounding: str,
        complex_turn: bool,
        emit: Optional[Callable[[dict], None]] = None,
    ) -> str:
        if not complex_turn:
            return draft
        if not settings.reply_pipeline_enabled:
            return draft

        max_rounds = settings.reply_pipeline_max_rounds
        mode = settings.selector_mode
        state = {
            "user_input": user_input,
            "grounding": grounding,
            "draft": draft,
            "verdict": None,
            "polished": None,
            "rounds": 0,
            "max_rounds": max_rounds,
        }
        selector = FunctionalSelector()

        # 总步数硬上限：防任何异常路径（例如规则/LLM 均判断异常）导致死循环。
        max_steps = max(max_rounds, 1) * 4 + 4
        for _ in range(max_steps):
            if mode == "llm":
                try:
                    role = selector.choose_llm(client, model, state)
                    reason = "llm_selector"
                except Exception:
                    role = selector.choose_rule(state)
                    reason = "llm_selector_invalid_fallback_rule"
            else:
                role = selector.choose_rule(state)
                reason = "rule_selector"

            # 达 max_rounds 后：选择器只允许 polish/done，强制收敛，防 LLM 无限重写。
            if state["rounds"] >= max_rounds and role in ("evaluate", "redraft"):
                role = "done" if state["polished"] is not None else "polish"
                reason = "max_rounds_reached_force_converge"

            _emit(emit, {"type": "select", "next": role, "reason": reason})

            if role == "done":
                break
            if role == "evaluate":
                self._evaluate(client, model, state, emit)
            elif role == "redraft":
                self._redraft(client, model, state, emit)
            elif role == "polish":
                self._polish(client, model, state, emit)
            else:
                break   # 理论不会发生：choose_rule/choose_llm 已收敛到四选一

        return state["polished"] if state["polished"] is not None else state["draft"]

    def _evaluate(self, client, model: str, state: dict, emit) -> None:
        messages = [
            {"role": "system", "content": EVALUATOR_PROMPT},
            {
                "role": "user",
                "content": (
                    f"用户问题：{state['user_input']}\n\n"
                    f"客服草稿：{state['draft']}\n\n"
                    f"本轮工具真实结果：{state['grounding'] or '（本轮未调用工具）'}"
                ),
            },
        ]
        try:
            resp = client.chat.completions.create(model=model, messages=messages, temperature=0.0)
            content = resp.choices[0].message.content or ""
            verdict = _parse_json(content)
            if "ok" not in verdict:
                raise ValueError("evaluator json missing 'ok'")
        except Exception:
            # fail-open 铁律：评估器坏 JSON / 异常，绝不阻断流程，视为通过。
            verdict = {"ok": True, "issues": [], "suggestion": ""}
        state["verdict"] = verdict
        state["rounds"] += 1
        _emit(emit, {"type": "evaluate", "ok": bool(verdict.get("ok", True))})

    def _redraft(self, client, model: str, state: dict, emit) -> None:
        verdict = state.get("verdict") or {}
        messages = [
            {"role": "system", "content": REDRAFT_PROMPT},
            {
                "role": "user",
                "content": (
                    f"用户问题：{state['user_input']}\n\n"
                    f"被打回的草稿：{state['draft']}\n\n"
                    f"评估问题：{verdict.get('issues', [])}\n"
                    f"改进建议：{verdict.get('suggestion', '')}\n\n"
                    f"本轮工具真实结果：{state['grounding'] or '（本轮未调用工具）'}"
                ),
            },
        ]
        try:
            resp = client.chat.completions.create(model=model, messages=messages, temperature=0.0)
            content = (resp.choices[0].message.content or "").strip()
            if content:
                state["draft"] = content
        except Exception:
            pass   # fail-open：重写失败，保留原草稿
        state["verdict"] = None   # 强制下一轮重新评估

    def _polish(self, client, model: str, state: dict, emit) -> None:
        messages = [
            {"role": "system", "content": POLISH_PROMPT},
            {
                "role": "user",
                "content": (
                    f"用户问题：{state['user_input']}\n\n"
                    f"待润色回复：{state['draft']}"
                ),
            },
        ]
        try:
            resp = client.chat.completions.create(model=model, messages=messages, temperature=0.0)
            content = (resp.choices[0].message.content or "").strip()
            state["polished"] = content if content else state["draft"]
        except Exception:
            state["polished"] = state["draft"]   # fail-open：润色失败，保留草稿/重写文本
        _emit(emit, {"type": "polish"})
