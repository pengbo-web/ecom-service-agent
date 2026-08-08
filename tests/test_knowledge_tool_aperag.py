"""search_knowledge 工具接入 ApeRAG:契约不变(名字/参数/返回形状)，
只是检索源按 settings.kb_backend 换。

覆盖点(对应任务的 Verify 清单)：
1. kb_backend=aperag 时确实调用 ApeRAG，并把行映射进既有返回形状；
2. top_k 依旧被 clamp，且原样传给 aperag_search；
3. 服务不可用(aperag_search 返回 None) 与 正常无命中([]) 两态可区分；
4. kb_backend=local 时行为与接入 ApeRAG 之前逐字节一致；
5. backend 字段老实汇报"这次实际是谁答的"，而不是恒等于配置项；
6. 维度/模型不匹配(EmbeddingIndexMismatchError)依旧原样往外抛，不被吞。
"""

import pytest

import app.agent.tools.knowledge as knowledge_mod
from app.agent.rag.errors import EmbeddingIndexMismatchError
from app.agent.tools.knowledge import search_knowledge
from app.config.settings import settings


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """每个用例前重置单例检索器 + kb_backend/降级开关,避免用例间串状态。"""
    knowledge_mod.reset_retriever()
    monkeypatch.setattr(settings, "kb_backend", "local")
    monkeypatch.setattr(settings, "kb_local_fallback_enabled", False)
    yield
    knowledge_mod.reset_retriever()


class _FakeChunk:
    def __init__(self, doc, section, text):
        self.doc = doc
        self.section = section
        self.text = text


class _FakeHit:
    def __init__(self, doc, section, text, score):
        self.chunk = _FakeChunk(doc, section, text)
        self.score = score


class _FakeRetriever:
    """替身本地检索器:只记录被调用的 query/top_k，返回预设 hits。"""

    def __init__(self, hits):
        self._hits = hits
        self.calls = []

    def search(self, query, top_k):
        self.calls.append((query, top_k))
        return self._hits


# ---------------------------------------------------------------------------
# 1&2. kb_backend=aperag:调用外部服务、映射行、top_k 传递与 clamp
# ---------------------------------------------------------------------------

def test_aperag_backend_maps_rows_into_existing_shape(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    captured = {}

    def fake_aperag_search(query, top_k=None):
        captured["query"] = query
        captured["top_k"] = top_k
        return [{"doc": "退换货政策", "section": "vector_search",
                 "score": 0.87, "text": "签收7天内可退"}]

    monkeypatch.setattr(knowledge_mod, "aperag_search", fake_aperag_search)
    result = search_knowledge("退货政策", top_k=2)

    assert result["success"] is True
    assert result["backend"] == "aperag"          # 老实汇报本轮实际后端
    assert result["query"] == "退货政策"
    assert result["results"] == [
        {"doc": "退换货政策", "section": "vector_search", "score": 0.87, "text": "签收7天内可退"}
    ]
    assert captured["query"] == "退货政策"
    assert captured["top_k"] == 2                 # top_k 原样透传给 aperag_search


def test_top_k_clamped_before_reaching_aperag_search(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    seen = []

    def fake_aperag_search(query, top_k=None):
        seen.append(top_k)
        return []

    monkeypatch.setattr(knowledge_mod, "aperag_search", fake_aperag_search)

    # 既有 clamp 表达式是 max(1, min(int(top_k or 3), 5))——0 是 falsy,
    # 会被 `or 3` 当成"未传"而不是"下限"，这是接入 ApeRAG 前就有的既定行为，
    # 这里用负数触发下限分支，不去动这条历史语义。
    search_knowledge("q", top_k=-1)     # clamp 到下限 1
    search_knowledge("q", top_k=99)     # clamp 到上限 5
    search_knowledge("q", top_k=3)      # 正常范围内不变
    assert seen == [1, 5, 3]


def test_aperag_results_sliced_to_top_k(monkeypatch):
    """即便外部服务返回条数超过 top_k(极端/上游融合异常)，也按 top_k 截断。"""
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    rows = [{"doc": f"d{i}", "section": "s", "score": 0.5, "text": "t"} for i in range(5)]
    monkeypatch.setattr(knowledge_mod, "aperag_search", lambda query, top_k=None: rows)
    result = search_knowledge("q", top_k=2)
    assert len(result["results"]) == 2


# ---------------------------------------------------------------------------
# 3. 服务不可用(None) vs 正常无命中([]) 两态可区分
# ---------------------------------------------------------------------------

def test_aperag_empty_list_is_success_with_no_results(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr(knowledge_mod, "aperag_search", lambda query, top_k=None: [])
    result = search_knowledge("无关问题")
    assert result["success"] is True               # 正常无命中,不是失败
    assert result["backend"] == "aperag"
    assert result["results"] == []


def test_aperag_none_unavailable_is_clean_failure_by_default(monkeypatch):
    """fallback 决策:默认(kb_local_fallback_enabled=False)干净失败，
    不抛异常、不悄悄说成"没查到"，success=False 且带 error，与上面的空命中态
    可区分。"""
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr(settings, "kb_local_fallback_enabled", False)
    monkeypatch.setattr(knowledge_mod, "aperag_search", lambda query, top_k=None: None)
    result = search_knowledge("退货政策")
    assert result["success"] is False
    assert result["backend"] == "aperag"
    assert result["results"] == []
    assert result["error"]                          # 有明确错误信息，不是空对空


def test_aperag_none_vs_empty_are_distinguishable(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr(settings, "kb_local_fallback_enabled", False)

    monkeypatch.setattr(knowledge_mod, "aperag_search", lambda query, top_k=None: None)
    unavailable = search_knowledge("q")

    monkeypatch.setattr(knowledge_mod, "aperag_search", lambda query, top_k=None: [])
    no_hit = search_knowledge("q")

    assert unavailable["success"] is False and unavailable["results"] == []
    assert no_hit["success"] is True and no_hit["results"] == []
    assert unavailable != no_hit


# ---------------------------------------------------------------------------
# 4&5. kb_backend=local:行为逐字节一致 + backend 字段仍是真实本地引擎名
# ---------------------------------------------------------------------------

def test_local_backend_behaves_exactly_as_before(monkeypatch):
    fake_retriever = _FakeRetriever([_FakeHit("退换货政策", "七天无理由", "本地命中", 0.91)])
    monkeypatch.setattr(knowledge_mod, "_get_retriever", lambda: fake_retriever)
    monkeypatch.setattr(settings, "rag_backend", "numpy")

    result = search_knowledge("退货政策", top_k=2)

    assert result["success"] is True
    assert result["backend"] == "numpy"            # 与接入 ApeRAG 之前一致:echo rag_backend
    assert result["results"] == [
        {"doc": "退换货政策", "section": "七天无理由", "score": 0.91, "text": "本地命中"}
    ]
    assert fake_retriever.calls == [("退货政策", 2)]


def test_local_backend_is_default_when_kb_backend_unset(monkeypatch):
    """settings.kb_backend 默认就是 local,不用显式切换也应走本地路径。"""
    assert settings.kb_backend == "local"
    fake_retriever = _FakeRetriever([])
    monkeypatch.setattr(knowledge_mod, "_get_retriever", lambda: fake_retriever)
    result = search_knowledge("随便问问")
    assert result["success"] is True
    assert result["results"] == []


# ---------------------------------------------------------------------------
# 6. aperag 故障 + 开启本地降级:实际服务谁答就报谁,且维度不匹配依旧原样抛出
# ---------------------------------------------------------------------------

def test_fallback_enabled_serves_local_and_reports_true_backend(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr(settings, "kb_local_fallback_enabled", True)
    monkeypatch.setattr(knowledge_mod, "aperag_search", lambda query, top_k=None: None)

    fake_retriever = _FakeRetriever([_FakeHit("退换货政策", "七天无理由", "本地兜底命中", 0.8)])
    monkeypatch.setattr(knowledge_mod, "_get_retriever", lambda: fake_retriever)
    monkeypatch.setattr(settings, "rag_backend", "chroma")

    result = search_knowledge("退货政策")

    assert result["success"] is True
    assert result["backend"] == "chroma"            # 真正回答的是本地 chroma,不是 "aperag"
    assert "本地兜底命中" in result["results"][0]["text"]


def test_dimension_mismatch_propagates_even_through_aperag_fallback_path(monkeypatch):
    """维度/模型不匹配是部署错误——无论走的是纯本地路径还是
    aperag 故障后降级本地的路径，都必须原样往外抛，不能被误判为
    "aperag 不可用"的常规容错吞掉。"""
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr(settings, "kb_local_fallback_enabled", True)
    monkeypatch.setattr(knowledge_mod, "aperag_search", lambda query, top_k=None: None)

    def boom():
        raise EmbeddingIndexMismatchError("索引维度与配置不一致,请重建")

    monkeypatch.setattr(knowledge_mod, "_get_retriever", boom)

    with pytest.raises(EmbeddingIndexMismatchError):
        search_knowledge("退货政策")


def test_dimension_mismatch_propagates_on_plain_local_path(monkeypatch):
    """kb_backend=local(默认)时同样不能被吞——覆盖 tests/test_rag_index_guard.py
    之外、走本模块新提取的 _search_local 辅助函数这条路径。"""
    def boom():
        raise EmbeddingIndexMismatchError("索引维度与配置不一致,请重建")

    monkeypatch.setattr(knowledge_mod, "_get_retriever", boom)

    with pytest.raises(EmbeddingIndexMismatchError):
        search_knowledge("退货政策")
