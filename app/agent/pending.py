"""挂起的待确认动作（Phase 4：服务端确认重放）。

问题：确认轮能否真正执行,依赖模型「确认后再次调用同一工具」——这是 LLM 行为,存在偶发不调。
方案：工具首次返回 need_confirm 时,把「工具名 + 真实参数 + 动作」按会话记下;
下一轮用户说确认语时,由服务端**确定性重放**该工具(见 streaming._replay_flow),不再赌模型会不会再调。

授权门(consent)保证「未确认不执行」,本模块保证「确认后必执行」——两者互补。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from app.agent.consent import RISK_ACTIONS


@dataclass(frozen=True)
class PendingAction:
    """一次被门控拦下、等待用户确认的风险动作。"""
    action: str          # consent 动作名,如 "refund" / "deal_close"
    tool_name: str       # 触发的工具名,如 "apply_refund" / "negotiate_price"
    args: dict           # 模型当时调用工具的真实参数,重放时原样复用
    message: str         # 工具返回的确认问题(已转达给用户)


class PendingActionStore:
    """按 session_id 保存最近一个待确认动作(内存,单进程)。"""

    def __init__(self):
        self._by_session: dict[str, PendingAction] = {}

    def remember(self, session_id: str, pending: PendingAction) -> None:
        if session_id:
            self._by_session[session_id] = pending

    def get(self, session_id: str) -> Optional[PendingAction]:
        return self._by_session.get(session_id)

    def pop(self, session_id: str) -> Optional[PendingAction]:
        return self._by_session.pop(session_id, None)

    def clear(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)


_store = PendingActionStore()


def get_pending_store() -> PendingActionStore:
    return _store


def set_pending_store(store: PendingActionStore) -> None:
    """测试用:替换全局 store。"""
    global _store
    _store = store


def observe_tool_result(session_id: str, tool_name: str, args: dict, result_str: str) -> None:
    """ReAct 循环执行完一个工具后调用:据结果维护挂起动作。

    - 工具返回 need_confirm=true → 记住这个待确认动作(带真实参数)。
    - 风险工具成功执行(success=true) → 清除该会话的挂起(已落地,无需重放)。
    其它工具/普通结果一律忽略,不影响核心流程。
    """
    if not session_id or not isinstance(result_str, str):
        return
    try:
        result = json.loads(result_str)
    except (ValueError, TypeError):
        return
    if not isinstance(result, dict):
        return

    if result.get("need_confirm") and result.get("action") in RISK_ACTIONS:
        _store.remember(session_id, PendingAction(
            action=result["action"],
            tool_name=tool_name,
            args=dict(args),
            message=result.get("message", ""),
        ))
    elif result.get("success") and tool_name in _RISK_TOOLS:
        _store.clear(session_id)


# 风险工具名 → 成功执行后清挂起
_RISK_TOOLS = frozenset({"apply_refund", "negotiate_price"})
