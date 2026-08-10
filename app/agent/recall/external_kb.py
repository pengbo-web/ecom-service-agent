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
from app.net.internal_http import internal_client

logger = logging.getLogger(__name__)


def aperag_search(query: str, top_k: int | None = None) -> list[dict] | None:
    """调用 ApeRAG collection-search API,返回归一化行或 None(服务不可用)。

    top_k:留空则沿用 settings.recall_kb_top_k(KB 预召回的既有调用方式，
    行为不变);显式传入时同时覆盖 vector_search/fulltext_search 的 topk——
    供 search_knowledge 工具把自己收到的 top_k 一路带下去。
    """
    if not settings.aperag_api_key or not settings.aperag_collection_id:
        logger.warning("kb_backend=aperag 但缺少 api_key/collection_id,降级本地")
        return None
    topk = top_k if isinstance(top_k, int) and top_k > 0 else settings.recall_kb_top_k
    url = (f"{settings.aperag_base_url.rstrip('/')}/api/v1/collections/"
           f"{settings.aperag_collection_id}/searches")
    payload = {
        "query": query,
        # similarity 在 OpenAPI 里标可选,但服务端 VectorSearchInput 必填——缺省会整体 500
        "vector_search": {"topk": topk,
                          "similarity": settings.aperag_min_similarity},
        "rerank": settings.aperag_rerank,
    }
    # settings.aperag_fulltext_enabled(默认关,见该设置项注释):实测这条腿对真实
    # 多字中文问句命中率为 0(中文分词没配好),却背了几乎全部尾部延迟——不发这条
    # 腿本身就是最直接的延迟修复。留着开关(不是删掉这条能力)是因为混合检索理论上
    # 确实优于纯向量单路,collection 配好中文分词后应该重新打开,到时候只需翻这一
    # 个开关,不用再改这里的调用形状。
    if settings.aperag_fulltext_enabled:
        payload["fulltext_search"] = {"topk": topk}
    try:
        # 内网地址绕过系统代理(`internal_client`,模块级导入)。这条是**买家每轮
        # 都走的热路径**:开发/部署机上挂着 HTTP_PROXY 而 NO_PROXY 没带回环时,
        # 每一次召回都被塞进代理隧道 → 502/超时,而本函数 fail-soft 返回 None,
        # 表现为"知识库里什么都没有"——客服照常回答,只是答案里没有任何政策依据。
        # 实测同一份代码在有 NO_PROXY 的终端里全通、在 uvicorn 进程里全挂。
        # 见 app/net/internal_http.py。
        with internal_client(url, timeout=settings.aperag_timeout_s) as c:
            resp = c.post(url, json=payload,
                          headers={"Authorization": f"Bearer {settings.aperag_api_key}"})
        if resp.status_code != 200:
            logger.warning("aperag search http %s: %s", resp.status_code, resp.text[:200])
            return None
        items = resp.json().get("items") or []
        rows = []
        for it in items:
            src = str(it.get("source") or "")
            rows.append({
                # 去 .md 后缀:与本地后端(stem)统一,客户可见的来源标注不带文件后缀
                "doc": (src.replace("\\", "/").rsplit("/", 1)[-1] or "知识库").removesuffix(".md"),
                "section": str(it.get("recall_type") or ""),
                "score": float(it.get("score") or 0.0),
                "text": str(it.get("content") or ""),
            })
        return rows
    except Exception:
        logger.warning("aperag search failed", exc_info=True)
        return None
