"""WS5 多轮用户模拟器原型(技术方案 §7)。

定位:为评测产生**多轮**流量。本项目评估集 10 条里只有 1 条多轮,而论文最有
说服力的失败模式——"Agent 第一轮就说反了规则,但用户没放弃,因为答案看起来
自洽"(错误的知识比缺失的知识更具欺骗性)——单轮 QA 几乎不可能触发。

**流量口径**:模拟流量标 `SOURCE_SIMULATED`,DECISION 与 SAMPLING 两套口径
**都排除**(runtime_context.py:129-133)——拿 skill 生成的对话再喂回去合成
门禁用例是闭环自证。它只服务评测与回归,永不进语料采样、永不进看门狗判定。

原型阶段用户侧**零 LLM**(scripted persona):多轮模拟估算 ~2500 次调用,
独立预算批下来之前只跑脚本版;LLM 驱动 persona 是后续阶段。停止条件全确定性:
goal_met / script_exhausted / gave_up / max_turns——模拟器的"什么时候闭嘴"
不由模型猜,与跟进序列终止条件同一条纪律。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

STOP_GOAL_MET = "goal_met"
STOP_SCRIPT_EXHAUSTED = "script_exhausted"
STOP_GAVE_UP = "gave_up"
STOP_MAX_TURNS = "max_turns"

DEFAULT_PERSONAS = Path(__file__).resolve().parent / "sim_personas.json"


@dataclass
class Persona:
    id: str
    opener: str
    followups: list[str] = field(default_factory=list)
    goal_keywords: list[str] = field(default_factory=list)
    patience: int = 2          # 最多容忍几轮"目标未达成",超出即 gave_up
    max_turns: int = 6


@dataclass
class SimTurn:
    user: str
    reply: str


@dataclass
class SimResult:
    persona_id: str
    turns: list[SimTurn]
    stop_reason: str

    @property
    def n_turns(self) -> int:
        return len(self.turns)


def run_persona(persona: Persona, chat_fn) -> SimResult:
    """驱动一个 persona 与 `chat_fn(user_text) -> reply_str` 对话。

    `chat_fn` 由调用方提供(沙箱 agent / 测试假 agent),本函数不碰 agent 构造
    与流量标记——隔离与口径是 `run_in_sandbox` 的职责。
    """
    turns: list[SimTurn] = []
    unsatisfied = 0
    user = persona.opener
    while True:
        reply = str(chat_fn(user) or "")
        turns.append(SimTurn(user, reply))
        if persona.goal_keywords and any(k and k in reply for k in persona.goal_keywords):
            return SimResult(persona.id, turns, STOP_GOAL_MET)
        if len(turns) >= persona.max_turns:
            return SimResult(persona.id, turns, STOP_MAX_TURNS)
        idx = len(turns) - 1
        if idx >= len(persona.followups):
            return SimResult(persona.id, turns, STOP_SCRIPT_EXHAUSTED)
        unsatisfied += 1
        if unsatisfied >= persona.patience:
            return SimResult(persona.id, turns, STOP_GAVE_UP)
        user = persona.followups[idx]


def load_personas(path=DEFAULT_PERSONAS) -> list[Persona]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Persona(**item) for item in data.get("personas", [])]


def run_in_sandbox(persona: Persona, sandbox=None, mode: str = "single") -> SimResult:
    """在评估沙箱隔离下跑一个 persona,流量标 SOURCE_SIMULATED。

    隔离复用 Sandbox 的既有机制(记忆/MCP 关、买家工具子集、补丁 finally 还原)
    ——那套机制的每条纪律(裸改 settings 不还曾把线上进程记忆永久关掉)不需要
    在这里重犯一遍。
    """
    from app.agent.runtime_context import SOURCE_SIMULATED, traffic_source_scope
    from app.evaluation.sandbox import Sandbox

    sb = sandbox or Sandbox(mode=mode)
    patches: list[tuple] = []
    try:
        with traffic_source_scope(SOURCE_SIMULATED):
            agent = sb._build_agent(sb.session_path_for(f"sim-{persona.id}"), patches)
            return run_persona(persona, lambda text: _reply_of(agent.chat(text)))
    finally:
        # 只还原补丁,**绝不调 agent.close()**——close 会触发 LTM 巩固的 LLM
        # 写入(sandbox.py 同一条纪律),而此刻补丁已还原、memory_enabled 回到
        # 真值,那一次写入会真的落进长期记忆。
        for obj, attr, original in patches:
            setattr(obj, attr, original)


def _reply_of(response) -> str:
    return str(getattr(response, "reply", "") or "")
