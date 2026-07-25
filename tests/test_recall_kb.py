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
