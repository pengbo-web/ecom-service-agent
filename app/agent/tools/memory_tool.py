"""记忆查询工具：让 Agent 在 ReAct 循环中主动查询用户记忆。

串户防护:SessionManager 会让多个用户的 agent 并存于同一进程,若用模块级
全局注入(只在 __init__ 一次),工具恒读到"最后构造的 agent"的记忆——A 用户
会话里可能查到 B 的长期记忆。故与 bargain.set_current_session 同模式:
ContextVar 存引用,EcomAgent.chat() 每轮开始时刷新为当前 agent 的 manager。
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.agent.memory.manager import MemoryManager

_memory_manager_var: contextvars.ContextVar[Optional["MemoryManager"]] = (
    contextvars.ContextVar("memory_manager", default=None)
)


def set_memory_manager(manager: Optional["MemoryManager"]) -> None:
    """注入当前轮的 MemoryManager。EcomAgent.__init__ 注入一次(单 agent 兼容),
    chat() 每轮再刷新(多 agent 并存时防串户)。"""
    _memory_manager_var.set(manager)


def _current_manager() -> Optional["MemoryManager"]:
    return _memory_manager_var.get()


def recall_user_memory(query: str = "") -> dict:
    """查询当前用户的记忆信息（长期记忆和短期记忆）。"""
    manager = _current_manager()
    if manager is None or not manager.memory_enabled:
        return {"success": False, "error": "记忆系统未启用"}

    result: dict = {
        "success": True,
        "short_term_facts": manager.stm.facts,
        "long_term_facts": [
            {"content": f.content, "category": f.category}
            for f in manager.ltm.facts
        ],
    }

    if manager.ltm.interaction_summaries:
        result["recent_interactions"] = [
            s["summary"] for s in manager.ltm.interaction_summaries[-3:]
        ]

    return result


# 合法的记忆类别及其含义,是这份口径唯一的权威来源。以前 registry.py 的
# save_user_memory 工具 schema 里还抄了一份 `enum: [...]` 快照给模型看,
# 新增/改名一个类别时那份快照没人记得同步,模型于是根本看不到新类别——
# 与 growth.py 的 OPPORTUNITY_KINDS 同一种病,治法也一样:registry.py 从
# 这里把说明文字派生进 description,不再维护第二份名单。
VALID_CATEGORIES: dict[str, str] = {
    "identity": "身份/会员",
    "preference": "偏好",
    "behavior": "行为习惯",
    "issue": "问题记录",
    "other": "其他",
}


def save_user_memory(content: str = "", category: str = "other") -> dict:
    """把用户明确表达的偏好/身份/重要事实即时写入长期记忆(跨会话立即生效)。

    三层写入的"显式即时写"层:与会话末 consolidate(隐式提取)、curation
    (策展治理)并存互补。写入即 save——FTS 同步索引,下一轮即可召回。
    """
    manager = _current_manager()
    if manager is None or not manager.memory_enabled:
        return {"success": False, "error": "记忆系统未启用"}
    content = (content or "").strip()
    if not content:
        return {"success": False, "error": "记忆内容不能为空"}
    content = content[:200]
    if category not in VALID_CATEGORIES:
        category = "other"

    try:
        from app.agent.tools.bargain import get_current_session
        source = get_current_session() or ""
    except Exception:
        source = ""

    from datetime import datetime
    from app.agent.memory.long_term import MemoryFact
    added = manager.ltm.add_facts([MemoryFact(
        content=content, category=category,
        created_at=datetime.now().isoformat(timespec="seconds"),
        source_session=source,
    )])
    already = added == 0        # 用真实新增数判重(长度比较在满 max_facts 裁剪时会撒谎)
    manager.ltm.save()          # 立即持久化 + FTS 重同步
    return {"success": True, "saved": content, "category": category,
            "already_known": already, "total_facts": len(manager.ltm.facts)}
