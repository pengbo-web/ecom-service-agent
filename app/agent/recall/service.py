"""统一召回服务(Recall Service):一个入口挂 N 个召回源。

生产对齐(阿里小蜜/京东言犀):**存储分离、召回统一**——
- 用户记忆(一人一份、可写、隐私)与平台知识库(全局共享、只读)存储各自独立;
- 回答前由本服务统一调度各源检索,合并为 system prompt 注入段。

源列表(注入顺序即代码顺序):
  profile     结构化用户档案(memory_profile_enabled 门控)
  long_term   长期记忆事实(FTS 召回命中优先)
  short_term  短期记忆滚动摘要
  kb          平台知识预检索(recall_kb_enabled 门控,独立于 memory_enabled)

隔离原则:任一源抛错只丢该源,不影响其它源与主流程。
"""

import logging
from dataclasses import dataclass, field

from app.config.settings import settings
from app.agent.recall.kb import KbRecall, kb_recall

logger = logging.getLogger(__name__)


@dataclass
class RecallResult:
    sections: list[dict] = field(default_factory=list)   # [{"role":"system","content":...}]
    kb_hits: list[dict] = field(default_factory=list)    # KB 命中明细(供 recall 事件/观测)


def _profile_section(memory_manager, query):
    if not settings.memory_profile_enabled:
        return None
    from app.agent.memory.profile import get_profile_store
    store = get_profile_store()
    if store is None:
        return None
    return store.get(memory_manager.ltm.user_id).to_prompt() or None


def _long_term_section(memory_manager, query):
    return memory_manager.ltm.build_prompt_section(query) or None


def _short_term_section(memory_manager, query):
    return memory_manager.stm.build_prompt_section() or None


def build_recall_sections(memory_manager, query: str | None) -> RecallResult:
    """统一召回入口:按源顺序检索,合并为注入段列表;单源失败隔离。"""
    result = RecallResult()
    memory_on = memory_manager is not None and getattr(memory_manager, "memory_enabled", False)
    if memory_on:
        for source in (_profile_section, _long_term_section, _short_term_section):
            try:
                text = source(memory_manager, query)
            except Exception:
                logger.warning("recall source %s failed", source.__name__, exc_info=True)
                continue
            if text:
                result.sections.append({"role": "system", "content": text})
    try:
        kb = kb_recall(query)
    except Exception:
        logger.warning("recall source kb failed", exc_info=True)
        kb = KbRecall()
    if kb.section:
        result.sections.append({"role": "system", "content": kb.section})
        result.kb_hits = kb.hits
    return result
