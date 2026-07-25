"""统一召回服务:多源合并/顺序/单源失败隔离/memory 开关独立性。"""

from types import SimpleNamespace

import app.agent.recall.service as svc
from app.agent.recall.kb import KbRecall
from app.config.settings import settings


def _mm(memory_enabled=True, ltm_text="记忆事实", stm_text="短期摘要", ltm_raises=False):
    def ltm_section(query):
        if ltm_raises:
            raise RuntimeError("fts broken")
        return ltm_text
    ltm = SimpleNamespace(user_id="u1", build_prompt_section=ltm_section)
    stm = SimpleNamespace(build_prompt_section=lambda: stm_text)
    return SimpleNamespace(memory_enabled=memory_enabled, ltm=ltm, stm=stm)


def test_merges_memory_and_kb_in_order(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    monkeypatch.setattr(svc, "kb_recall",
                        lambda q: KbRecall(section="【平台知识(自动检索)】KB段",
                                           hits=[{"doc": "d", "section": "s", "score": 0.5}]))
    r = svc.build_recall_sections(_mm(), "退货政策")
    assert [x["content"] for x in r.sections] == ["记忆事实", "短期摘要", "【平台知识(自动检索)】KB段"]
    assert all(x["role"] == "system" for x in r.sections)
    assert r.kb_hits == [{"doc": "d", "section": "s", "score": 0.5}]


def test_memory_off_kb_still_works(monkeypatch):
    """存储分离的意义:平台知识召回不受用户记忆开关影响。"""
    monkeypatch.setattr(svc, "kb_recall", lambda q: KbRecall(section="KB段", hits=[]))
    r = svc.build_recall_sections(_mm(memory_enabled=False), "退货政策")
    assert [x["content"] for x in r.sections] == ["KB段"]


def test_memory_source_error_isolated(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    monkeypatch.setattr(svc, "kb_recall", lambda q: KbRecall())
    r = svc.build_recall_sections(_mm(ltm_raises=True), "q")
    assert [x["content"] for x in r.sections] == ["短期摘要"]   # LTM 坏了只丢 LTM


def test_kb_error_isolated(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    def boom(q):
        raise RuntimeError("kb down")
    monkeypatch.setattr(svc, "kb_recall", boom)
    r = svc.build_recall_sections(_mm(), "q")
    assert [x["content"] for x in r.sections] == ["记忆事实", "短期摘要"]
    assert r.kb_hits == []


def test_empty_sources_yield_empty(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    monkeypatch.setattr(svc, "kb_recall", lambda q: KbRecall())
    r = svc.build_recall_sections(_mm(ltm_text="", stm_text=""), "q")
    assert r.sections == [] and r.kb_hits == []


def test_none_memory_manager_kb_only(monkeypatch):
    monkeypatch.setattr(svc, "kb_recall", lambda q: KbRecall(section="KB段", hits=[]))
    r = svc.build_recall_sections(None, "q")
    assert [x["content"] for x in r.sections] == ["KB段"]
