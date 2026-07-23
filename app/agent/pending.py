"""挂起的待确认动作(R3:随会话状态持久化,确认前重启也不丢)。

工具首次返回 need_confirm 时,把「动作 / 工具名 / 真实参数 / 确认问题」记进 **会话状态**
(agent._pending,随 SessionStore 落盘);下一轮用户说确认语时,streaming 用它由服务端
**确定性重放**该工具(见 streaming._replay_flow)。

授权门(consent)保证「未确认不执行」,本机制保证「确认后必执行」——两者互补。
R3 前挂起动作放在进程内全局 store(重启即丢);现在放进会话状态,跨重启/跨实例(Redis)不丢。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Optional

from app.agent.consent import RISK_ACTIONS

# 风险工具名 → 成功执行后清挂起
_RISK_TOOLS = frozenset({"apply_refund", "negotiate_price", "cancel_order", "change_address"})


@dataclass
class PendingAction:
    """一次被门控拦下、等待用户确认的风险动作。"""
    action: str          # consent 动作名,如 "refund" / "cancel_order"
    tool_name: str       # 触发的工具名,如 "apply_refund"
    args: dict           # 模型当时调用工具的真实参数,重放时原样复用
    message: str         # 工具返回的确认问题(已转达给用户)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PendingAction":
        return cls(
            action=d["action"], tool_name=d["tool_name"],
            args=dict(d.get("args") or {}), message=d.get("message", ""),
        )


def evaluate_pending(tool_name: str, args: dict, result_str: str):
    """据一次工具返回,决定挂起动作如何变化(纯函数,由 agent 应用到 self._pending):

    - ("set", PendingAction) —— 工具返回 need_confirm 且为风险动作 → 记住待确认(带真实参数)。
    - ("clear", None)        —— 风险工具成功执行 → 清除挂起(已落地,无需重放)。
    - ("keep", None)         —— 其它情况不变。
    """
    if not isinstance(result_str, str):
        return ("keep", None)
    try:
        result = json.loads(result_str)
    except (ValueError, TypeError):
        return ("keep", None)
    if not isinstance(result, dict):
        return ("keep", None)

    if result.get("need_confirm") and result.get("action") in RISK_ACTIONS:
        return ("set", PendingAction(
            action=result["action"], tool_name=tool_name,
            args=dict(args), message=result.get("message", ""),
        ))
    if result.get("success") and tool_name in _RISK_TOOLS:
        return ("clear", None)
    return ("keep", None)
