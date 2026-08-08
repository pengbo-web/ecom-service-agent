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
from app.agent.recall.kb import KbRecall, kb_recall, kb_format

logger = logging.getLogger(__name__)


@dataclass
class RecallResult:
    sections: list[dict] = field(default_factory=list)   # [{"role":"system","content":...}]
    kb_hits: list[dict] = field(default_factory=list)    # KB 命中明细(供 recall 事件/观测)
    kb_backend: str = "local"                            # 本轮 KB 实际后端(aperag/local),供前端标识来源
    # 本轮 ApeRAG 调用的耗时/结果观测(从 KbRecall.latency 原样搬上来);None=
    # backend!="aperag" 或本轮压根没检索,见 KbRecall.latency 的文档。
    kb_latency: dict | None = None


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


def build_recall_sections(memory_manager, query: str | None,
                          include_kb: bool = True,
                          kb_domain: str | None = None,
                          kb_prefetch: tuple[list[dict], str] | tuple[list[dict], str, dict | None]
                          | None = None) -> RecallResult:
    """统一召回入口:按源顺序检索,合并为注入段列表;单源失败隔离。
    include_kb=False(查询理解判定本轮无需知识)时跳过 KB 源,记忆源照常。

    kb_prefetch(L3①):调用方(EcomAgent._build_messages)已经用**同一个** query
    并发取到的 (rows, backend),这里只需按 kb_domain 做本地排序/裁剪/格式化
    (kb_format),不再重新发起检索请求——与直接调 kb_recall(query, kb_domain)
    相比,只是把"取行"这一步挪到了更早、并发的时间点,格式化尾部代码完全
    共用,因此两条路径在同一 query 下产出逐字节一致(不改变检索结果,只改
    变检索发起的时间点)。None(默认)= 老行为,现场调 kb_recall。

    R1 补充:当本轮的并发预取**已经发起过**一次检索但没拿到结果(失败/早退)
    时,调用方(EcomAgent._build_messages)会用 include_kb=False 调本函数并在
    返回后把 kb_backend 改标为 "unavailable"——既不能用 kb_prefetch(没有行),
    也**不能**在这里现场再检索一次(那就是第二次阻塞检索,正是 R1 要消灭的
    东西),按全局约束"检索失败保持非致命:买家仍然拿到回复,只是这一轮没有
    知识注入"处理。"""
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
    if not include_kb:
        result.kb_backend = "skipped"
        return result
    try:
        if kb_prefetch is not None:
            # *_rest 容错解包:kb_prefetch 现在可能是 (rows, backend, meta) 三元组
            # (预取带上了 ApeRAG 耗时观测),但仍兼容既有测试直接喂 (rows, backend)
            # 二元组的写法——两种都能跑,只是二元组时 meta 留 None。
            rows, backend, *_rest = kb_prefetch
            kb = kb_format(rows, backend, kb_domain)
            kb.latency = _rest[0] if _rest else None
        else:
            kb = kb_recall(query, domain=kb_domain)
    except Exception:
        logger.warning("recall source kb failed", exc_info=True)
        kb = KbRecall()
    result.kb_backend = kb.backend
    result.kb_latency = kb.latency
    if kb.section:
        result.sections.append({"role": "system", "content": kb.section})
        result.kb_hits = kb.hits
    return result
