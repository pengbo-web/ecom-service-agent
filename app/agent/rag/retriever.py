"""知识库检索器：query → 向量化 → 委托后端检索。

设计上 retriever 只负责"问句怎么变向量""结果怎么聚合"，
存储和打分都交给 VectorBackend 实现，对应两套：
- NumpyBackend：手写余弦 + JSON 持久化（教学透明，零依赖）
- ChromaBackend：嵌入式向量数据库 + HNSW（生产代表性）

校验逻辑：加载后比对 backend 持久化的 embedding_model 与当前 Embedder.model，
不一致直接报错——避免"换了 embedding 但还在用老索引"这种隐蔽问题。
"""

from __future__ import annotations

from app.agent.rag.backends.base import RetrievedChunk, VectorBackend
from app.agent.rag.embedder import Embedder
from app.agent.rag.errors import EmbeddingIndexMismatchError
from app.config.settings import settings

__all__ = ["KnowledgeRetriever", "RetrievedChunk"]


class KnowledgeRetriever:
    """对上层暴露统一接口，对下委托给具体 backend。"""

    def __init__(self, embedder: Embedder, backend: VectorBackend):
        self._embedder = embedder
        self._backend = backend
        self._loaded = False

    @property
    def backend(self) -> VectorBackend:
        return self._backend

    @property
    def size(self) -> int:
        return self._backend.size()

    def load(self) -> None:
        if self._loaded:
            return
        self._backend.load()

        # 下面两处不一致都抛 EmbeddingIndexMismatchError,不能是普通异常——
        # 上层 search_knowledge() 会显式放行这个类型，不把它并进常规的
        # "embedding 失败=本轮无知识库"fail-soft 兜底(见 app/agent/rag/errors.py)。
        expected_model = self._backend.expected_embedding_model()
        if expected_model and expected_model != self._embedder.model:
            raise EmbeddingIndexMismatchError(
                f"知识库索引的 embedding 模型({expected_model!r})与当前配置"
                f"(settings.embedding_model={self._embedder.model!r})不一致。"
                f"请先运行 `python app/scripts/build_kb_index.py` 用当前模型"
                f"重建索引，不能直接拿旧模型的向量继续检索(维度/语义空间都"
                f"不一样，相似度会静默算错)。"
            )
        expected_dim = self._backend.expected_embedding_dim()
        if expected_dim and expected_dim != settings.embedding_dimension:
            raise EmbeddingIndexMismatchError(
                f"知识库索引的向量维度({expected_dim})与当前配置"
                f"(settings.embedding_dimension={settings.embedding_dimension})不一致。"
                f"请先运行 `python app/scripts/build_kb_index.py` 用当前模型重建索引。"
            )
        self._loaded = True

    def search(self, query: str, top_k: int = 3) -> list[RetrievedChunk]:
        if not self._loaded:
            self.load()
        q_vec = self._embedder.encode_one(query)
        return self._backend.search(q_vec, top_k=top_k)
