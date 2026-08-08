"""知识库向量索引:持久化 embedding 模型/维度 + 加载期不一致校验(W1 L1)。

全离线单测,不打真实 API——用一个"假 Embedder"只声明 `.model`,不真的调网络。
覆盖 NumpyBackend 与 KnowledgeRetriever 两层:后端只管"持久化文件里记的是什么",
retriever 再叠一层"跟当前 Embedder/配置比对"。
"""

import json

import pytest

from app.agent.rag.backends.numpy_backend import NumpyBackend
from app.agent.rag.chunker import Chunk
from app.agent.rag.errors import EmbeddingIndexMismatchError
from app.agent.rag.retriever import KnowledgeRetriever
from app.config.settings import settings


class _FakeEmbedder:
    """只声明 model 名,search()/encode_one() 不会被下面这些用例真正调用到。"""

    def __init__(self, model: str):
        self.model = model

    def encode_one(self, text):
        raise AssertionError("维度/模型不匹配应该在 load() 就报错，不该走到这里")


def _write_index(path, embedding_model: str, embedding_dim: int, n_vectors: int = 2):
    chunks = [Chunk(chunk_id=f"c{i}", doc="d", section="s", text="t") for i in range(n_vectors)]
    payload = {
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "chunks": [c.to_dict() for c in chunks],
        "vectors": [[0.1] * embedding_dim for _ in range(n_vectors)],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# NumpyBackend:持久化 + 回读 embedding_model / embedding_dim
# ---------------------------------------------------------------------------

def test_upsert_persists_model_and_dim(tmp_path):
    backend = NumpyBackend(tmp_path / "idx.json")
    chunks = [Chunk(chunk_id="c1", doc="d", section="s", text="t")]
    backend.upsert(chunks, vectors=[[0.1, 0.2, 0.3]], embedding_model="m1")
    data = json.loads((tmp_path / "idx.json").read_text(encoding="utf-8"))
    assert data["embedding_model"] == "m1"
    assert data["embedding_dim"] == 3


def test_load_recovers_dim_for_legacy_file_without_dim_field(tmp_path):
    """升级前构建的索引没有 embedding_dim 字段:用实际向量长度兜底，而不是
    留 0(那样等于放行了"其实是旧维度"的索引，正是本任务要根除的静默 bug)。"""
    idx = tmp_path / "idx.json"
    chunks = [Chunk(chunk_id="c1", doc="d", section="s", text="t")]
    idx.write_text(json.dumps({
        "embedding_model": "old-model",
        "chunks": [c.to_dict() for c in chunks],
        "vectors": [[0.1] * 1536],
    }), encoding="utf-8")
    backend = NumpyBackend(idx)
    backend.load()
    assert backend.expected_embedding_dim() == 1536


# ---------------------------------------------------------------------------
# KnowledgeRetriever.load():模型名 / 维度 两处校验，均报 EmbeddingIndexMismatchError
# ---------------------------------------------------------------------------

def test_load_raises_on_model_name_mismatch(tmp_path):
    idx = tmp_path / "idx.json"
    _write_index(idx, embedding_model="old-model", embedding_dim=settings.embedding_dimension)
    backend = NumpyBackend(idx)
    retriever = KnowledgeRetriever(embedder=_FakeEmbedder(settings.embedding_model), backend=backend)
    with pytest.raises(EmbeddingIndexMismatchError, match="重建索引"):
        retriever.load()


def test_load_raises_on_dim_mismatch_even_if_model_name_matches(tmp_path):
    """模型名对得上但维度对不上(比如同名模型换了输出维度的极端情况)也必须
    拦住——只查模型名不够，这就是要求"模型+维度都记录、都校验"的原因。"""
    idx = tmp_path / "idx.json"
    _write_index(idx, embedding_model=settings.embedding_model, embedding_dim=1536)
    backend = NumpyBackend(idx)
    retriever = KnowledgeRetriever(embedder=_FakeEmbedder(settings.embedding_model), backend=backend)
    with pytest.raises(EmbeddingIndexMismatchError, match="维度"):
        retriever.load()


def test_load_succeeds_when_model_and_dim_match(tmp_path):
    idx = tmp_path / "idx.json"
    _write_index(idx, embedding_model=settings.embedding_model,
                 embedding_dim=settings.embedding_dimension)
    backend = NumpyBackend(idx)
    retriever = KnowledgeRetriever(embedder=_FakeEmbedder(settings.embedding_model), backend=backend)
    retriever.load()          # 不应抛
    assert retriever.size == 2


def test_search_knowledge_reraises_mismatch_instead_of_degrading(tmp_path, monkeypatch):
    """search_knowledge() 是 KB 预召回/工具调用共用的入口——维度不匹配必须
    原样往外抛，不能被这里的常规容错包成 success=False 悄悄降级。"""
    import app.agent.tools.knowledge as knowledge_mod

    idx = tmp_path / "idx.json"
    _write_index(idx, embedding_model="old-model", embedding_dim=settings.embedding_dimension)
    monkeypatch.setattr(settings, "kb_index_path", str(idx))
    monkeypatch.setattr(settings, "rag_backend", "numpy")
    knowledge_mod.reset_retriever()
    try:
        with pytest.raises(EmbeddingIndexMismatchError):
            knowledge_mod.search_knowledge("随便问问", top_k=1)
    finally:
        knowledge_mod.reset_retriever()
