"""KB 预召回源:阈值过滤/字符预算/门控/容错/embedding 失败可见性(W1 L1)。"""

import pytest

import app.agent.recall.kb as kb_mod
from app.agent.rag.errors import EmbeddingIndexMismatchError
from app.agent.recall.kb import kb_recall
from app.config.settings import settings
from app.observability import embedding_health


def _fake_results(*rows):
    return {"success": True, "backend": "numpy", "query": "q",
            "results": [{"doc": d, "section": s, "score": sc, "text": t}
                        for d, s, sc, t in rows]}


def test_hit_builds_section_and_hits(monkeypatch):
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("退换货政策", "七天无理由", 0.62, "签收7天内可退")))
    r = kb_recall("退货政策是什么")
    assert r.section is not None
    assert "签收7天内可退" in r.section
    assert "退换货政策/七天无理由" in r.section
    assert "平台知识" in r.section          # 注入段有明确标头,提示词规则按此标头引用
    assert r.hits == [{"doc": "退换货政策", "section": "七天无理由", "score": 0.62}]


def test_below_threshold_filtered(monkeypatch):
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("FAQ", "无关", 0.10, "无关内容")))
    r = kb_recall("退货政策是什么")
    assert r.section is None and r.hits == []


def test_disabled_gate(monkeypatch):
    monkeypatch.setattr(settings, "recall_kb_enabled", False)
    called = []
    monkeypatch.setattr(kb_mod, "search_knowledge", lambda q, top_k: called.append(q))
    assert kb_recall("退货政策是什么").section is None
    assert called == []                     # 关开关连检索都不发起


def test_short_or_empty_query_skipped(monkeypatch):
    called = []
    monkeypatch.setattr(kb_mod, "search_knowledge", lambda q, top_k: called.append(q))
    assert kb_recall("嗯").section is None      # 短于 recall_kb_min_query_chars
    assert kb_recall(None).section is None
    assert kb_recall("   ").section is None
    assert called == []


def test_search_error_returns_empty(monkeypatch):
    def boom(q, top_k):
        raise RuntimeError("embedding down")
    monkeypatch.setattr(kb_mod, "search_knowledge", boom)
    r = kb_recall("退货政策是什么")
    assert r.section is None and r.hits == []   # 容错:失败不抛出


def test_search_failure_dict_returns_empty(monkeypatch):
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: {"success": False, "error": "索引缺失", "results": []})
    assert kb_recall("退货政策是什么").section is None


def test_char_budget_drops_tail(monkeypatch):
    long_text = "长" * 1200
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("政策", "A", 0.9, "短片段"),
                                                       ("政策", "B", 0.8, long_text)))
    r = kb_recall("退货政策是什么")
    assert "短片段" in r.section and long_text not in r.section
    assert [h["section"] for h in r.hits] == ["A"]   # 超预算片段连 hits 也不记


def test_backend_dispatch_aperag_used_when_available(monkeypatch):
    import app.agent.recall.kb as kb_mod2
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search",
                        lambda q: [{"doc": "退换货政策.md", "section": "vector_search",
                                    "score": 0.9, "text": "外部命中"}])
    local_called = []
    monkeypatch.setattr(kb_mod2, "search_knowledge",
                        lambda q, top_k: local_called.append(q))
    r = kb_recall("退货政策是什么")
    assert "外部命中" in r.section and r.backend == "aperag"
    assert local_called == []                        # 外部可用则不碰本地


def test_aperag_empty_hits_does_not_degrade_to_local(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search", lambda q: [])
    local_called = []
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: local_called.append(q))
    r = kb_recall("退货政策是什么")
    assert r.section is None and r.backend == "aperag"
    assert local_called == []            # [] = 正常无命中,不降级本地


def test_backend_dispatch_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr(settings, "kb_local_fallback_enabled", True)   # 兜底需显式开
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search", lambda q: None)
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("退换货政策", "七天", 0.8, "本地兜底命中")))
    r = kb_recall("退货政策是什么")
    assert "本地兜底命中" in r.section and r.backend == "local"


def test_fallback_disabled_by_default_no_injection(monkeypatch):
    """默认关兜底:aperag 故障时本轮无注入(体验纯 ApeRAG,故障可感知),本地零流量。"""
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search", lambda q: None)
    local_called = []
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: local_called.append(q))
    r = kb_recall("退货政策是什么")
    assert r.section is None and r.backend == "aperag"
    assert local_called == []              # 关兜底连本地都不碰


def test_backend_default_local_untouched(monkeypatch):
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("退换货政策", "七天", 0.8, "本地命中")))
    r = kb_recall("退货政策是什么")
    assert "本地命中" in r.section and r.backend == "local"


def test_search_error_is_counted(monkeypatch):
    """W1 L1:embedding 调用失败即使继续 fail-soft 返回空结果,也必须留下计数
    痕迹——过去这里只有一条 logger.warning,没人在盯日志就等于没发生过。"""
    embedding_health.reset_embedding_failure_stats()

    def boom(q, top_k):
        raise RuntimeError("404 model_not_found")

    monkeypatch.setattr(kb_mod, "search_knowledge", boom)
    r = kb_recall("退货政策是什么")
    assert r.section is None                                   # fail-soft 行为不变
    stats = embedding_health.embedding_failure_stats()
    assert stats["total"] == 1 and stats["by_source"]["kb_recall"] == 1
    embedding_health.reset_embedding_failure_stats()


def test_degraded_success_false_is_also_counted(monkeypatch):
    embedding_health.reset_embedding_failure_stats()
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: {"success": False, "error": "知识库初始化失败: 404", "results": []})
    kb_recall("退货政策是什么")
    stats = embedding_health.embedding_failure_stats()
    assert stats["total"] == 1 and stats["by_source"]["kb_recall"] == 1
    embedding_health.reset_embedding_failure_stats()


def test_index_mismatch_propagates_not_swallowed(monkeypatch):
    """维度/模型不匹配是结构性部署错误,不应该在这一层被当成"本轮无知识库"
    的常规容错吞掉——必须原样往外抛(build_recall_sections 的单源隔离契约
    是另一层的取舍，不在本模块内提前吞掉)。"""
    def boom(q, top_k):
        raise EmbeddingIndexMismatchError("索引维度与配置不一致,请重建")

    monkeypatch.setattr(kb_mod, "search_knowledge", boom)
    with pytest.raises(EmbeddingIndexMismatchError):
        kb_recall("退货政策是什么")


def test_aperag_rows_skip_min_score_but_respect_budget(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    long_text = "长" * 1200
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search",
                        lambda q: [{"doc": "a.md", "section": "fulltext_search",
                                    "score": 0.05, "text": "低分但服务端已排序"},
                                   {"doc": "b.md", "section": "vector_search",
                                    "score": 0.9, "text": long_text}])
    r = kb_recall("退货政策是什么")
    assert "低分但服务端已排序" in r.section          # 外部行不做 min_score 过滤
    assert long_text not in r.section                # 预算仍生效
