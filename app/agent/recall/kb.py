"""KB 预召回源:统一召回层的"平台知识"通道。

存储分离、召回统一(对齐阿里小蜜/京东言犀模式):知识库仍是全局共享只读存储,
本模块只负责在每轮回答前**主动**检索并格式化为可注入的 system prompt 段,
不再依赖模型自觉调用 search_knowledge 工具(实测触发率低,是政策编造的空档)。

容错原则:预召回是增强,不是依赖——任何失败(索引缺失/embedding 超时)都
返回空结果,绝不阻塞回复主流程。
"""

import logging
from dataclasses import dataclass, field

from app.config.settings import settings
from app.agent.tools.knowledge import search_knowledge

logger = logging.getLogger(__name__)


@dataclass
class KbRecall:
    section: str | None = None                       # 可注入的 system prompt 段;无命中为 None
    hits: list[dict] = field(default_factory=list)   # [{"doc","section","score"}] 供事件/观测展示
    backend: str = "local"                           # 本轮实际使用的后端(local/aperag),供观测


_HEADER = (
    "【平台知识(自动检索)】以下片段来自平台官方知识库,按与本轮用户问题的相关度自动检索;"
    "与当前问题无关时忽略。回答政策/规则问题时优先引用以下内容,不得与之矛盾:"
)


def _local_rows(query: str) -> list[dict]:
    """本地向量索引取行;失败返回 []。"""
    try:
        result = search_knowledge(query, top_k=settings.recall_kb_top_k)
    except Exception:
        logger.warning("kb pre-recall search failed", exc_info=True)
        return []
    if not result.get("success"):
        logger.warning("kb pre-recall degraded: %s", result.get("error"))
        return []
    return result.get("results", [])


def _fetch_rows(query: str) -> tuple[list[dict], str]:
    """按 kb_backend 取行。aperag 故障时:开 kb_local_fallback_enabled 则降级本地索引
    (三级降级 aperag→local→无注入);关(默认)则本轮直接无注入——体验纯 ApeRAG 行为,
    降级不再被本地兜底悄悄掩盖,故障在前端表现为该轮没有「预召回」行。"""
    if settings.kb_backend == "aperag":
        from app.agent.recall.external_kb import aperag_search
        rows = aperag_search(query)
        if rows is not None:
            return rows, "aperag"
        if not settings.kb_local_fallback_enabled:
            logger.warning("kb backend aperag unavailable, local fallback DISABLED -> no injection this turn")
            return [], "aperag"
        logger.warning("kb backend aperag unavailable, fallback to local index")
    return _local_rows(query), "local"


def kb_recall(query: str | None, domain: str | None = None) -> KbRecall:
    """对本轮用户问题做 KB 预检索,返回格式化注入段与命中明细。"""
    if not settings.recall_kb_enabled:
        return KbRecall()
    if not query or len(query.strip()) < settings.recall_kb_min_query_chars:
        return KbRecall()
    rows, backend = _fetch_rows(query)
    from app.agent.recall.kb_tags import rank_by_domain
    rows = rank_by_domain(rows, domain)

    lines: list[str] = []
    hits: list[dict] = []
    used = len(_HEADER)
    for r in rows:
        if backend == "local" and r.get("score", 0.0) < settings.recall_kb_min_score:
            continue
        line = f"- [{r.get('doc', '')}/{r.get('section', '')}] {r.get('text', '')}"
        if used + len(line) > settings.recall_kb_max_chars:
            break
        lines.append(line)
        used += len(line)
        hits.append({"doc": r.get("doc", ""), "section": r.get("section", ""),
                     "score": r.get("score", 0.0)})
    if not lines:
        return KbRecall(backend=backend)
    return KbRecall(section=_HEADER + "\n" + "\n".join(lines), hits=hits, backend=backend)
