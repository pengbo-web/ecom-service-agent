"""知识库检索工具：通过向量检索回答 FAQ、政策类问题。

与查订单/查物流这类「结构化数据查询」工具不同，
search_knowledge 面向非结构化文本（退换货政策、配送说明、FAQ 等），
返回 Top-K 命中片段及其来源，由 LLM 引用回答。

检索源由 settings.kb_backend 切换（ApeRAG 接入，第一步：只换检索源，
不改工具的名字/参数/返回形状，五个 persona 与 process-return 等 skill
无需改动）：
- local ：项目内向量索引，行为与接入 ApeRAG 之前逐字节一致
- aperag：外部 ApeRAG（向量+全文混合检索），不可用时是否降级本地由
          settings.kb_local_fallback_enabled 决定（见 _search_aperag 旁注）

local 分支下，向量后端再由 settings.rag_backend 切换：
- numpy ：手写余弦相似度，零依赖，教学透明
- chroma：嵌入式向量数据库，HNSW 索引，工程代表性

为避免每次进程启动都重建索引，单例缓存 Retriever。
"""

import logging
from pathlib import Path
from typing import Optional

from app.config.settings import settings
from app.agent.rag.backends import create_backend
from app.agent.rag.embedder import Embedder
from app.agent.rag.errors import EmbeddingIndexMismatchError
from app.agent.rag.retriever import KnowledgeRetriever
from app.agent.recall.external_kb import aperag_search
from app.observability.embedding_health import record_embedding_failure

logger = logging.getLogger(__name__)

_retriever: Optional[KnowledgeRetriever] = None


def _create_backend_from_settings():
    """根据 settings.rag_backend 创建对应后端实例。"""
    name = settings.rag_backend.lower()
    if name == "numpy":
        return create_backend("numpy", index_path=Path(settings.kb_index_path))
    if name == "chroma":
        return create_backend(
            "chroma",
            persist_dir=Path(settings.chroma_persist_dir),
            collection_name=settings.chroma_collection,
        )
    raise ValueError(
        f"未知的 RAG 后端: {settings.rag_backend}（可选: numpy / chroma）"
    )


def _get_retriever() -> KnowledgeRetriever:
    global _retriever
    if _retriever is None:
        embedder = Embedder(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.embedding_model,
            # 热路径客户端(预召回+工具检索共用):显式短超时+零重试,挂起不能拖垮回复
            timeout=settings.recall_kb_timeout_s,
            max_retries=settings.recall_kb_embed_retries,
        )
        backend = _create_backend_from_settings()
        _retriever = KnowledgeRetriever(embedder=embedder, backend=backend)
        _retriever.load()
    return _retriever


def reset_retriever() -> None:
    """清空单例缓存（测试或切换后端时使用）。"""
    global _retriever
    _retriever = None


def warm_local_retriever() -> None:
    """启动预热用:构造并加载本地向量索引 + embedder 客户端(process 级单例，
    见 `_get_retriever`)。只碰本地文件/构造 HTTP 客户端，绝不发真实网络请求
    (embedder 只在真正 encode 时才调用 Embeddings 接口)，也绝不触达外部
    ApeRAG——`kb_backend=aperag` 时本地索引只是三级降级的最后一级，与
    ApeRAG 是否可达无关。

    本地索引文件不存在(从未跑过 `build_kb_index.py`)是正常状态而非故障，
    这里不做特殊吞掉——直接让 `FileNotFoundError` 原样抛给调用方；
    预热框架(`app/api/warmup.py`)本来就按步骤各自 try/except，一步"没有
    可暖的东西"不该算成功也不该拖垮其它预热步骤。
    """
    _get_retriever()


def _search_local(query: str, top_k: int) -> dict:
    """kb_backend=local 时的检索路径。

    与接入 ApeRAG 之前逐字节一致——把原来 search_knowledge 里的全部逻辑
    原样搬到这里，供默认路径与 aperag 故障降级路径共用同一份实现，避免
    两处各写一遍、行为跑偏。top_k 由调用方(search_knowledge)clamp 好后传入。
    """
    try:
        retriever = _get_retriever()
    except FileNotFoundError as e:
        return {
            "success": False,
            "error": str(e),
            "backend": settings.rag_backend,
            "query": query,
            "results": [],
        }
    except EmbeddingIndexMismatchError:
        # 维度/模型不匹配是部署错误，不是瞬时故障——绝不能在这里当成
        # "本轮无知识库"悄悄兜底，必须原样往外抛，让调用方明确失败。
        raise
    except Exception as e:
        record_embedding_failure("kb_search", e)
        return {
            "success": False,
            "error": f"知识库初始化失败: {e}",
            "backend": settings.rag_backend,
            "query": query,
            "results": [],
        }

    try:
        hits = retriever.search(query, top_k=top_k)
    except EmbeddingIndexMismatchError:
        raise
    except Exception as e:
        record_embedding_failure("kb_search", e)
        return {
            "success": False,
            "error": f"知识库检索失败: {e}",
            "backend": settings.rag_backend,
            "query": query,
            "results": [],
        }

    return {
        "success": True,
        "backend": settings.rag_backend,
        "query": query,
        "results": [
            {
                "doc": h.chunk.doc,
                "section": h.chunk.section,
                "score": round(h.score, 4),
                "text": h.chunk.text,
            }
            for h in hits
        ],
    }


def _search_aperag(query: str, top_k: int) -> Optional[dict]:
    """kb_backend=aperag 时的检索路径。返回 None 表示服务不可用(由调用方决定
    是否降级本地)，否则返回已套好本工具既有返回形状的结果字典。

    aperag_search 内部已经把"未配置/连接拒/超时/非200/解析错"统一兜底为
    None——这里不重复判断失败原因，只做"服务给出结果" → 映射成
    doc/section/score/text 标准行，与本地路径的返回形状完全一致，模型/
    调用方无需区分来源。ApeRAG 服务端按 vector_search/fulltext_search 各自
    的 topk 融合排序，理论上不会超过 top_k，这里仍显式切片一次做兜底，
    确保 top_k 语义在两条路径上一致可信。
    """
    rows = aperag_search(query, top_k=top_k)
    if rows is None:
        return None
    return {
        "success": True,
        "backend": "aperag",
        "query": query,
        "results": [
            {
                "doc": r.get("doc", ""),
                "section": r.get("section", ""),
                "score": round(float(r.get("score", 0.0)), 4),
                "text": r.get("text", ""),
            }
            for r in rows[:top_k]
        ],
    }


def search_knowledge(query: str, top_k: int = 3) -> dict:
    """检索退换货政策、配送说明、会员权益、FAQ 等知识库内容。

    检索源由 settings.kb_backend 决定：
    - aperag：先查外部 ApeRAG；服务不可用(返回 None)时，
      settings.kb_local_fallback_enabled 决定是否降级本地索引再试一次，
      默认关闭——参见模块顶部与下方降级决策的注释。
    - local (默认)：与接入 ApeRAG 之前完全一致，走本地向量索引。

    Returns:
        {
          "success": bool,
          "backend": "aperag" | "numpy" | "chroma",  # 老实汇报"这次实际是谁答的"
          "query": str,
          "results": [
            {"doc": "...", "section": "...", "score": 0.83, "text": "..."},
            ...
          ],
          "error": "..."  # 仅失败时存在
        }
    """
    if not query or not query.strip():
        return {"success": False, "error": "query 不能为空", "query": query, "results": []}

    top_k = max(1, min(int(top_k or 3), 5))

    if settings.kb_backend == "aperag":
        result = _search_aperag(query, top_k)
        if result is not None:
            return result
        # 降级决策(记录在案，非拍脑袋)：
        # 本方法是五个 persona 都挂着的 agent 可调用工具，process-return 等
        # skill 把"调用 search_knowledge 检索政策"写成必经步骤——工具在这里
        # 硬失败，意味着 ApeRAG 一旦不可用，退货退款这类流程当场卡死，用户
        # 侧体验是"客服突然答不出退货政策"。但 owner 的既定方向是最终删掉
        # 本地 RAG，本方法现在加的任何兜底代码都是将来要删的技术债。
        # 权衡结果：默认不新增行为——遵循 kb.py(统一召回层)已经用同一个
        # settings.kb_local_fallback_enabled 定的口径，两条调用路径(工具
        # 直调 / 预召回)行为一致，不搞两套降级策略。默认 False：
        # ApeRAG 不可用时工具"干净失败"(success=False + error，不抛异常、
        # 不把"服务挂了"悄悄说成"没查到政策")，保持"纯 ApeRAG 体验，故障
        # 可感知"，也不多留一份将来必须清理的本地兜底代码路径。运维/owner
        # 想在 ApeRAG 不稳的过渡期临时保住退货流程不中断，可随时把这一个
        # 已存在的开关打开(True)，工具会和 kb.py 一样降级本地索引再试一次
        # ——是否降级仍完全由这一个配置项决定，不是本方法自己另起一套判断。
        if settings.kb_local_fallback_enabled:
            logger.warning("search_knowledge: ApeRAG 不可用，降级本地索引")
            return _search_local(query, top_k)
        return {
            "success": False,
            "error": "ApeRAG 服务不可用(kb_backend=aperag 且未开启本地降级)",
            "backend": "aperag",
            "query": query,
            "results": [],
        }

    return _search_local(query, top_k)
