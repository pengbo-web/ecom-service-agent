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


def _loaded_variant(result_str: str) -> str:
    """从 load_skill 返回里取本轮实际加载的版本(live/canary);缺失按 live。"""
    from app.agent.skills.canary import VARIANT_LIVE

    try:
        data = json.loads(result_str)
    except (ValueError, TypeError):
        return VARIANT_LIVE
    if not isinstance(data, dict):
        return VARIANT_LIVE
    return str(data.get("variant") or VARIANT_LIVE)


def _loaded_version(result_str: str) -> int:
    """从 load_skill 返回里取本轮实际加载的**技能目录版本号**;缺失/坏数据按 0(未知)。

    这是加载那一刻读到的值,随 tool_call 结果一起进轨迹——绝不在落库时重新读磁盘
    (中间可能已经转正,那样记的就是错的版本)。
    """
    try:
        data = json.loads(result_str)
    except (ValueError, TypeError):
        return 0
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def _loaded_fingerprint(result_str: str) -> str:
    """从 load_skill 返回里取本轮实际加载的**内容指纹**;缺失/坏数据按 "unknown"。

    与 `_loaded_version` 同一条纪律:取的是加载那一刻返回值里带的字符串,不在
    落库时重新对磁盘现算——中途若发生转正,现算就会算出下一版的指纹,那就不是
    "这一轮实际执行的是哪棵树"了。
    """
    from app.agent.skills.versioning import UNKNOWN_FINGERPRINT

    try:
        data = json.loads(result_str)
    except (ValueError, TypeError):
        return UNKNOWN_FINGERPRINT
    if not isinstance(data, dict):
        return UNKNOWN_FINGERPRINT
    fp = data.get("skill_fingerprint")
    return str(fp).strip() if fp else UNKNOWN_FINGERPRINT


@dataclass
class SkillTurn:
    """单轮对话的 skill 执行轨迹。未加载 skill 的轮次不会被落库(has_skill=False)。"""

    skill_name: str = ""
    loaded_skills: list[str] = field(default_factory=list)   # 本轮加载过的**全部** skill(守卫要对每个都判)
    tool_calls: list[dict] = field(default_factory=list)
    variant: str = "live"   # 本轮实际加载的版本(灰度期可能是 canary)
    skill_version: int = 0   # 本轮加载那一刻的技能目录版本号,0=未知(见 set_version)
    skill_fingerprint: str = "unknown"   # 本轮加载那一刻的内容指纹,见 set_fingerprint

    def note_tool_call(self, name: str, result_str: str, args: dict | None = None) -> None:
        """记录一次工具调用。若是成功的 load_skill,同时记下 skill 名与加载的版本。

        args 一并记录:G1 守卫要据此判定"前置调用的参数与本次是否一致"。
        """
        ok, error = _parse_result(result_str)
        if name == LOAD_SKILL_TOOL and ok:
            loaded = _loaded_skill_name(result_str)
            if loaded:
                self.skill_name = loaded
                self.variant = _loaded_variant(result_str)
                self.set_version(_loaded_version(result_str))
                self.set_fingerprint(_loaded_fingerprint(result_str))
                if loaded not in self.loaded_skills:
                    self.loaded_skills.append(loaded)
        self.tool_calls.append({"name": name, "ok": ok, "error": error,
                                "args": dict(args or {})})

    def note_preloaded(self, skill_name: str, variant: str = "live") -> None:
        """记录服务端**确定性预加载**的 skill(非模型调用,故不产生 tool_call 条目)。

        必须记进来:守卫按 loaded_skills 逐个判定、轨迹按 skill_name 归因——
        少了这一步,预加载等于白做。
        """
        if not skill_name:
            return
        self.skill_name = skill_name
        if skill_name not in self.loaded_skills:
            self.loaded_skills.append(skill_name)
        self.variant = variant or "live"

    def set_version(self, v: int) -> None:
        """记下本轮加载 skill 时的版本号(加载那一刻的值)。

        必须在加载发生的那一刻调用、把结果**带着走**——绝不能等到落库时才现读
        磁盘上的版本号,中间可能已经发生转正,那样记下的就是错的版本。
        """
        self.skill_version = v or 0

    def set_fingerprint(self, fp: str) -> None:
        """记下本轮加载 skill 时的内容指纹(加载那一刻的值)。

        与 set_version 同一条纪律:必须在加载发生的那一刻带着走,不能等落库时
        再现算——现算不仅慢(重新遍历目录哈希全部文件),中途还可能已经转正/
        灰度切换,现算出来的就不是这一轮实际执行的那棵树的指纹了。
        """
        from app.agent.skills.versioning import UNKNOWN_FINGERPRINT

        self.skill_fingerprint = fp or UNKNOWN_FINGERPRINT

    def note_blocked(self, name: str, args: dict | None, reason: str) -> None:
        """记录一次被工作流守卫拦下的调用(未执行)。

        blocked 条目不计入 tool_error:守卫拦住跳步、模型随后补齐并成功,是守卫
        起作用而非本轮失败。若计入失败,守卫越有效灰度成功率越难看。
        """
        self.tool_calls.append({"name": name, "ok": False, "error": reason,
                                "args": dict(args or {}), "blocked": True})

    @property
    def blocked_count(self) -> int:
        return sum(1 for call in self.tool_calls if call.get("blocked"))

    def outcome(self, requires_human: bool) -> str:
        """本轮结局:转人工 > 真实工具失败 > 成功(守卫拦截不算失败)。"""
        if requires_human:
            return OUTCOME_HANDOFF
        if any(not call["ok"] and not call.get("blocked") for call in self.tool_calls):
            return OUTCOME_TOOL_ERROR
        return OUTCOME_SUCCESS

    @property
    def has_skill(self) -> bool:
        return bool(self.skill_name)
