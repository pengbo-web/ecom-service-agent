"""FAQ 语义缓存:命中/阈值/容错/单例。"""

import app.agent.faq_cache as fc
from app.agent.faq_cache import FaqCache
from app.config.settings import settings


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
