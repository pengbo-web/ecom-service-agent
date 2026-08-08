"""FAQ 语义缓存:命中/阈值/容错/单例/三态区分/维度模型加载期校验(W1 L1)。"""

import json

import pytest

import app.agent.faq_cache as fc
from app.agent.faq_cache import FaqCache
from app.agent.rag.errors import EmbeddingIndexMismatchError
from app.config.settings import settings
from app.observability import embedding_health


def _cache(tmp_path, monkeypatch, entries=None):
    monkeypatch.setattr(settings, "faq_cache_enabled", True)
    c = FaqCache(str(tmp_path / "faq.json"))
    # 打桩 embedding:确定性向量,免网络
    vecs = {"下单后多久发货": [1.0, 0.0], "下单多久能发货?": [0.98, 0.199],
            "价保多久": [0.0, 1.0], "完全无关的问题": [0.7, 0.714]}
    monkeypatch.setattr(c, "_embed", lambda t: vecs.get(t, [0.5, 0.5]))
    for q, a in (entries or []):
        c.add(q, a)
    return c


def test_hit_above_threshold(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    hit = c.lookup("下单多久能发货?")
    assert hit is not None and hit["answer"] == "48小时内出库"
    assert hit["question"] == "下单后多久发货" and hit["score"] >= 0.9


def test_below_threshold_misses(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    assert c.lookup("价保多久") is None


def test_disabled_returns_none(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    monkeypatch.setattr(settings, "faq_cache_enabled", False)
    assert c.lookup("下单多久能发货?") is None


def test_embed_failure_returns_none(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    def boom(t):
        raise RuntimeError("embedding down")
    monkeypatch.setattr(c, "_embed", boom)
    assert c.lookup("下单多久能发货?") is None      # 容错:失败=未命中


def test_persistence_roundtrip(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    c2 = FaqCache(str(tmp_path / "faq.json"))       # 重新加载文件
    monkeypatch.setattr(c2, "_embed", lambda t: [0.98, 0.199])
    assert c2.lookup("下单多久能发货?")["answer"] == "48小时内出库"


def test_corrupt_file_tolerated(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    c = FaqCache(str(p))                             # 不抛,空缓存
    assert c.entries == []


def test_schema_broken_entry_never_raises(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch)
    c.entries = [{"q": "只有问题没答案", "emb": [1.0, 0.0]}]
    monkeypatch.setattr(c, "_embed", lambda t: [1.0, 0.0])
    assert c.lookup("只有问题没答案") is None      # 完美命中但缺答案→安全None


def test_singleton_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "faq_cache_path", str(tmp_path / "s.json"))
    fc.reset_faq_cache()
    a = fc.get_faq_cache()
    assert a is fc.get_faq_cache()
    fc.reset_faq_cache()
    assert a is not fc.get_faq_cache()


# ---------------------------------------------------------------------------
# W1 L1:持久化文件记录 embedding 模型/维度 + 加载期不一致校验
# ---------------------------------------------------------------------------

def test_save_persists_current_model_and_dim(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    data = json.loads((tmp_path / "faq.json").read_text(encoding="utf-8"))
    assert data["embedding_model"] == settings.embedding_model
    assert data["embedding_dim"] == settings.embedding_dimension


def test_load_raises_on_model_mismatch(tmp_path, monkeypatch):
    p = tmp_path / "mismatch.json"
    p.write_text(json.dumps({
        "embedding_model": "some-other-model",
        "embedding_dim": settings.embedding_dimension,
        "entries": [{"q": "q", "a": "a", "emb": [0.1] * settings.embedding_dimension}],
    }), encoding="utf-8")
    with pytest.raises(EmbeddingIndexMismatchError, match="重建"):
        FaqCache(str(p))


def test_load_raises_on_dim_mismatch(tmp_path, monkeypatch):
    p = tmp_path / "mismatch.json"
    p.write_text(json.dumps({
        "embedding_model": settings.embedding_model,
        "embedding_dim": 1536,
        "entries": [{"q": "q", "a": "a", "emb": [0.1] * 1536}],
    }), encoding="utf-8")
    with pytest.raises(EmbeddingIndexMismatchError, match="重建"):
        FaqCache(str(p))


def test_load_tolerates_legacy_file_without_metadata(tmp_path, monkeypatch):
    """升级前的老文件没有 embedding_model/embedding_dim 字段:按"未知,不校验"
    处理,不能因为加了新校验就把历史文件直接炸掉。"""
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps({"entries": [{"q": "q", "a": "a", "emb": [0.1, 0.2]}]}),
                 encoding="utf-8")
    c = FaqCache(str(p))            # 不应抛
    assert len(c.entries) == 1


def test_load_matching_metadata_does_not_raise(tmp_path, monkeypatch):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dimension,
        "entries": [],
    }), encoding="utf-8")
    FaqCache(str(p))                # 不应抛


# ---------------------------------------------------------------------------
# W1 L1:三态区分(hit / miss / unavailable)+ 失败计数
# ---------------------------------------------------------------------------

def test_lookup_with_state_hit(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    outcome = c.lookup_with_state("下单多久能发货?")
    assert outcome.state == "hit"
    assert outcome.hit["answer"] == "48小时内出库"
    assert outcome.error is None


def test_lookup_with_state_miss(tmp_path, monkeypatch):
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])
    outcome = c.lookup_with_state("价保多久")
    assert outcome.state == "miss"
    assert outcome.hit is None


def test_lookup_with_state_unavailable_on_embed_failure(tmp_path, monkeypatch):
    """三态的关键一条:embedding 调用失败必须是 unavailable,不能跟 miss 混在
    一起——这正是过去 FAQ 缓存整体失效很久没人发现的原因。"""
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])

    def boom(t):
        raise RuntimeError("404 model_not_found")

    monkeypatch.setattr(c, "_embed", boom)
    outcome = c.lookup_with_state("下单多久能发货?")
    assert outcome.state == "unavailable"
    assert "404" in outcome.error
    # 兼容旧接口:lookup() 仍然只返回 None,不区分 miss/unavailable
    assert c.lookup("下单多久能发货?") is None


def test_embed_failure_is_counted(tmp_path, monkeypatch):
    """embedding 失败必须留下计数痕迹,不能"发生了没人知道"。"""
    embedding_health.reset_embedding_failure_stats()
    c = _cache(tmp_path, monkeypatch, [("下单后多久发货", "48小时内出库")])

    def boom(t):
        raise RuntimeError("boom")

    monkeypatch.setattr(c, "_embed", boom)
    c.lookup_with_state("下单多久能发货?")
    stats = embedding_health.embedding_failure_stats()
    assert stats["total"] == 1
    assert stats["by_source"]["faq_cache"] == 1
    assert "boom" in stats["last_error"]

    c.lookup_with_state("再来一次")
    assert embedding_health.embedding_failure_stats()["total"] == 2
    embedding_health.reset_embedding_failure_stats()
