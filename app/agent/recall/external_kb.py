"""ApeRAG 外部检索源:统一召回层 KB 源的 external 后端。

调用本机 ApeRAG(用户参与的开源 RAG 平台)collection searches API——
向量+全文双路混合检索,服务端融合排序,可选重排。返回与本地后端相同的
标准化行,由 kb.py 统一做预算/格式化。

约定:None=服务不可用(kb.py 降级本地索引);[]=服务正常但无命中(不降级)。
容错红线:任何失败(连接拒/超时/非200/解析错)都只 warning + 返回 None。
"""

import logging

import httpx

from app.config.settings import settings

logger = logging.getLogger(__name__)


def aperag_search(query: str) -> list[dict] | None:
    if not settings.aperag_api_key or not settings.aperag_collection_id:
        logger.warning("kb_backend=aperag 但缺少 api_key/collection_id,降级本地")
        return None
    url = (f"{settings.aperag_base_url.rstrip('/')}/api/v1/collections/"
           f"{settings.aperag_collection_id}/searches")
    payload = {
        "query": query,
        "vector_search": {"topk": settings.recall_kb_top_k},
        "fulltext_search": {"topk": settings.recall_kb_top_k},
        "rerank": settings.aperag_rerank,
    }
    try:
        resp = httpx.post(url, json=payload,
                          headers={"Authorization": f"Bearer {settings.aperag_api_key}"},
                          timeout=settings.recall_kb_timeout_s)
        if resp.status_code != 200:
            logger.warning("aperag search http %s: %s", resp.status_code, resp.text[:200])
            return None
        items = resp.json().get("items") or []
    except Exception:
        logger.warning("aperag search failed", exc_info=True)
        return None
    rows = []
    for it in items:
        src = str(it.get("source") or "")
        rows.append({
            "doc": src.replace("\\", "/").rsplit("/", 1)[-1] or "知识库",
            "section": str(it.get("recall_type") or ""),
            "score": float(it.get("score") or 0.0),
            "text": str(it.get("content") or ""),
        })
    return rows
