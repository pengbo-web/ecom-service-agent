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
                        lambda q, domain=None: KbRecall(section="【平台知识(自动检索)】KB段",
                                                        hits=[{"doc": "d", "section": "s", "score": 0.5}]))
    r = svc.build_recall_sections(_mm(), "退货政策")
    assert [x["content"] for x in r.sections] == ["记忆事实", "短期摘要", "【平台知识(自动检索)】KB段"]
    assert all(x["role"] == "system" for x in r.sections)
    assert r.kb_hits == [{"doc": "d", "section": "s", "score": 0.5}]


def test_memory_off_kb_still_works(monkeypatch):
    """存储分离的意义:平台知识召回不受用户记忆开关影响。"""
    monkeypatch.setattr(svc, "kb_recall", lambda q, domain=None: KbRecall(section="KB段", hits=[]))
    r = svc.build_recall_sections(_mm(memory_enabled=False), "退货政策")
    assert [x["content"] for x in r.sections] == ["KB段"]


def test_memory_source_error_isolated(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    monkeypatch.setattr(svc, "kb_recall", lambda q, domain=None: KbRecall())
    r = svc.build_recall_sections(_mm(ltm_raises=True), "q")
    assert [x["content"] for x in r.sections] == ["短期摘要"]   # LTM 坏了只丢 LTM


def test_kb_error_isolated(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    def boom(q, domain=None):
        raise RuntimeError("kb down")
    monkeypatch.setattr(svc, "kb_recall", boom)
    r = svc.build_recall_sections(_mm(), "q")
    assert [x["content"] for x in r.sections] == ["记忆事实", "短期摘要"]
    assert r.kb_hits == []


def test_empty_sources_yield_empty(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    monkeypatch.setattr(svc, "kb_recall", lambda q, domain=None: KbRecall())
    r = svc.build_recall_sections(_mm(ltm_text="", stm_text=""), "q")
    assert r.sections == [] and r.kb_hits == []


def test_none_memory_manager_kb_only(monkeypatch):
    monkeypatch.setattr(svc, "kb_recall", lambda q, domain=None: KbRecall(section="KB段", hits=[]))
    r = svc.build_recall_sections(None, "q")
    assert [x["content"] for x in r.sections] == ["KB段"]


def test_include_kb_false_skips_kb_entirely(monkeypatch):
    """检索门控:include_kb=False 时连 kb_recall 都不调,记忆源照常。"""
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    called = []
    monkeypatch.setattr(svc, "kb_recall", lambda q, domain=None: called.append(q))
    r = svc.build_recall_sections(_mm(), "好的", include_kb=False)
    assert [x["content"] for x in r.sections] == ["记忆事实", "短期摘要"]
    assert r.kb_hits == [] and r.kb_backend == "skipped"
    assert called == []


# ---- L3①:kb_prefetch(并发预取复用)与现场检索必须产出逐字节一致的结果 ----

def test_kb_prefetch_matches_fresh_kb_recall_for_same_query(monkeypatch):
    """核心正确性证明:同一个 query,"直接调 kb_recall"和"先 kb_fetch_rows
    再喂给 build_recall_sections 的 kb_prefetch"必须产出完全相同的 RecallResult
    ——这是"并发不改变检索结果"这条约束的直接测试(不打真实网络,只验证
    两条路径共用同一段格式化代码)。"""
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    import app.agent.recall.kb as kb_mod
    monkeypatch.setattr(kb_mod, "search_knowledge",
                        lambda q, top_k: {"success": True, "results": [
                            {"doc": "退换货政策", "section": "七天无理由",
                             "score": 0.8, "text": "签收7天内可退"}]})

    rows_backend = kb_mod.kb_fetch_rows("退货政策是什么")
    assert rows_backend is not None
    # 3 元组:_fetch_rows 现在多带一个 meta(ApeRAG 耗时观测,backend=="local"
    # 时为 None)——这里只关心 rows/backend,meta 用 *_ 接住不解包。
    rows, backend, *_ = rows_backend

    r_fresh = svc.build_recall_sections(None, "退货政策是什么", kb_domain="aftersale")
    r_prefetch = svc.build_recall_sections(None, "退货政策是什么", kb_domain="aftersale",
                                           kb_prefetch=(rows, backend))
    assert r_fresh.sections == r_prefetch.sections
    assert r_fresh.kb_hits == r_prefetch.kb_hits
    assert r_fresh.kb_backend == r_prefetch.kb_backend


def test_kb_prefetch_used_does_not_call_kb_recall(monkeypatch):
    """有可复用的预取时,build_recall_sections 不该再触发一次现场检索
    (svc.kb_recall 完全不该被调用)——否则"并发省时间"这件事就是假的。"""
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    called = []
    monkeypatch.setattr(svc, "kb_recall", lambda q, domain=None: called.append(q))
    r = svc.build_recall_sections(
        None, "q", kb_domain=None,
        kb_prefetch=([{"doc": "d", "section": "s", "score": 0.9, "text": "命中内容"}], "local"))
    assert called == []                      # 没有现场检索
    assert "命中内容" in r.sections[0]["content"]


def test_kb_prefetch_none_falls_back_to_fresh_kb_recall(monkeypatch):
    """kb_prefetch=None(未开并发/未命中预取)时行为与改造前完全一致。"""
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    called = []
    monkeypatch.setattr(svc, "kb_recall", lambda q, domain=None: called.append(q) or KbRecall())
    svc.build_recall_sections(None, "q", kb_domain=None, kb_prefetch=None)
    assert called == ["q"]
