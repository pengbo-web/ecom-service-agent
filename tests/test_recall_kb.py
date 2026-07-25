"""KB 预召回源:阈值过滤/字符预算/门控/容错。"""

import app.agent.recall.kb as kb_mod
from app.agent.recall.kb import kb_recall
from app.config.settings import settings


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


def test_backend_dispatch_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(settings, "kb_backend", "aperag")
    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search", lambda q: None)
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("退换货政策", "七天", 0.8, "本地兜底命中")))
    r = kb_recall("退货政策是什么")
    assert "本地兜底命中" in r.section and r.backend == "local"


def test_backend_default_local_untouched(monkeypatch):
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: _fake_results(("退换货政策", "七天", 0.8, "本地命中")))
    r = kb_recall("退货政策是什么")
    assert "本地命中" in r.section and r.backend == "local"


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
