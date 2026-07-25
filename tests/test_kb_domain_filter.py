"""意图过滤检索:boost 前置/strict 过滤带回退/off 原样/未知文档不误杀。"""

import app.agent.recall.kb as kb_mod
from app.agent.recall.kb import kb_recall
from app.agent.recall.kb_tags import rank_by_domain
from app.config.settings import settings

ROWS = [
    {"doc": "优惠券与促销规则.md", "section": "s", "score": 0.9, "text": "券"},
    {"doc": "退换货政策.md", "section": "s", "score": 0.8, "text": "退"},
    {"doc": "神秘新文档.md", "section": "s", "score": 0.7, "text": "未知"},
]


def test_boost_moves_matching_first(monkeypatch):
    monkeypatch.setattr(settings, "recall_domain_mode", "boost")
    out = rank_by_domain(list(ROWS), "aftersale")
    assert out[0]["doc"] == "退换货政策.md"           # 匹配域前置
    assert len(out) == 3                              # 不丢行
    assert out[1]["doc"] == "优惠券与促销规则.md"      # 其余保持原相对顺序


def test_strict_filters_but_falls_back_when_empty(monkeypatch):
    monkeypatch.setattr(settings, "recall_domain_mode", "strict")
    out = rank_by_domain(list(ROWS), "aftersale")
    docs = [r["doc"] for r in out]
    assert "退换货政策.md" in docs and "优惠券与促销规则.md" not in docs
    assert "神秘新文档.md" in docs                     # 未知标签文档不误杀
    only_presale = [{"doc": "退换货政策.md", "section": "s", "score": 0.8, "text": "退"}]
    out2 = rank_by_domain(only_presale, "presale")
    assert out2 == only_presale                        # 过滤为空→回退全量


def test_off_mode_and_none_domain_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "recall_domain_mode", "off")
    assert rank_by_domain(list(ROWS), "aftersale") == ROWS
    monkeypatch.setattr(settings, "recall_domain_mode", "boost")
    assert rank_by_domain(list(ROWS), None) == ROWS


def test_kb_recall_applies_domain(monkeypatch):
    monkeypatch.setattr(settings, "recall_domain_mode", "boost")
    monkeypatch.setattr(kb_mod, "_fetch_rows",
                        lambda q: (list(ROWS), "local"))
    monkeypatch.setattr(settings, "recall_kb_min_score", 0.0)
    monkeypatch.setattr(settings, "recall_kb_min_query_chars", 0)   # "退货"仅2字,放开短查询门槛
    r = kb_recall("退货", domain="aftersale")
    assert r.hits[0]["doc"] == "退换货政策.md"
