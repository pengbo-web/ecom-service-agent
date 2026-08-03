"""G2 Skill 执行轨迹:记录每轮"加载了哪个 skill / 调了哪些工具 / 结局如何"。

用途:让自进化的采样源从"猜首条消息关键词"升级为"读真实执行轨迹"——
G3 按 outcome 采失败案例、G4 门禁按 skill 维度看成功率。

本模块是纯逻辑(无 IO、无 settings 依赖),落库由调用方(chat.py 回合结束)完成。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

LOAD_SKILL_TOOL = "load_skill"

OUTCOME_SUCCESS = "success"
OUTCOME_TOOL_ERROR = "tool_error"
OUTCOME_HANDOFF = "handoff"


def _parse_result(result_str: str) -> tuple[bool, str | None]:
    """从工具返回的 JSON 串判定成败。

    - `{"success": false, ...}` / 含 `error` 字段 → 失败,带错误文案。
    - 非 JSON / 无这两个字段 → 无法判定,按成功计(不制造假失败)。
    - `success` 字段优先于 `error`:两者同时出现时,`success: true` 权威,
      即使带 `error`(警告类文案)也判成功,不制造假失败。
    """
    try:
        data = json.loads(result_str)
    except (ValueError, TypeError):
        return True, None
    if not isinstance(data, dict):
        return True, None

    if data.get("success") is False:
        return False, str(data.get("error") or data.get("message") or "")
    if data.get("success") is True:
        return True, None          # success 为真时权威:即使带 error 字段(警告类)也算成功
    if data.get("error"):
        return False, str(data["error"])
    return True, None


def _loaded_skill_name(result_str: str) -> str:
    """从 load_skill 的成功返回里取出 skill 名;取不到返回空串。"""
    try:
        data = json.loads(result_str)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("skill_name") or "").strip()


@dataclass
class SkillTurn:
    """单轮对话的 skill 执行轨迹。未加载 skill 的轮次不会被落库(has_skill=False)。"""

    skill_name: str = ""
    tool_calls: list[dict] = field(default_factory=list)

    def note_tool_call(self, name: str, result_str: str) -> None:
        """记录一次工具调用。若是成功的 load_skill,同时记下本轮加载的 skill 名。"""
        ok, error = _parse_result(result_str)
        if name == LOAD_SKILL_TOOL and ok:
            loaded = _loaded_skill_name(result_str)
            if loaded:
                self.skill_name = loaded
        self.tool_calls.append({"name": name, "ok": ok, "error": error})

    def outcome(self, requires_human: bool) -> str:
        """本轮结局:转人工 > 工具失败 > 成功(转人工是更强的负信号,优先)。"""
        if requires_human:
            return OUTCOME_HANDOFF
        if any(not call["ok"] for call in self.tool_calls):
            return OUTCOME_TOOL_ERROR
        return OUTCOME_SUCCESS

    @property
    def has_skill(self) -> bool:
        return bool(self.skill_name)
