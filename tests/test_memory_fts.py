"""MemoryFtsStore 测试:验证中文关键词召回、user_id 隔离、top_k、去重。

文件后半段(H2.2)追加 LongTermMemory 接入 FTS 的集成测试:写入同步、recall、
命中优先注入、门控回退、策展替换后索引不漂移、reset 清索引。全部离线,
不触网、不调用真 LLM。
"""

from __future__ import annotations

from app.agent.memory.fts_store import MemoryFtsStore
from app.agent.memory.long_term import LongTermMemory, MemoryFact
from app.config.settings import settings


def _make_store(tmp_path):
    db_path = str(tmp_path / "mem_fts.db")
    return MemoryFtsStore(db_path)


def test_chinese_keyword_recall_hits_expected_fact(tmp_path):
    store = _make_store(tmp_path)
    store.index("u1", "f1", "用户喜欢红色运动鞋")
    store.index("u1", "f2", "用户是钻石会员")
    store.index("u1", "f3", "用户经常询问物流进度")

    results = store.search("u1", "红色")
    assert "用户喜欢红色运动鞋" in results

    results = store.search("u1", "会员")
    assert "用户是钻石会员" in results


def test_no_match_returns_empty_list(tmp_path):
    store = _make_store(tmp_path)
    store.index("u1", "f1", "用户喜欢红色运动鞋")
    store.index("u1", "f2", "用户是钻石会员")
    store.index("u1", "f3", "用户经常询问物流进度")

    results = store.search("u1", "手机")
    assert results == []


def test_user_isolation(tmp_path):
    store = _make_store(tmp_path)
    store.index("userA", "f1", "用户喜欢红色运动鞋")
    store.index("userB", "f1", "用户喜欢蓝色帽子")

    results_a = store.search("userA", "红色")
    assert "用户喜欢红色运动鞋" in results_a
    assert "用户喜欢蓝色帽子" not in results_a

    results_b = store.search("userB", "红色")
    assert results_b == []


def test_top_k_limits_result_count(tmp_path):
    store = _make_store(tmp_path)
    store.index("u1", "f1", "用户喜欢红色运动鞋")
    store.index("u1", "f2", "用户喜欢红色外套")
    store.index("u1", "f3", "用户喜欢红色帽子")
    store.index("u1", "f4", "用户喜欢红色围巾")

    results = store.search("u1", "红色", top_k=2)
    assert len(results) <= 2


def test_reindex_same_fact_id_does_not_duplicate(tmp_path):
    store = _make_store(tmp_path)
    store.index("u1", "f1", "用户喜欢红色运动鞋")
    store.index("u1", "f1", "用户喜欢红色运动鞋")
    store.index("u1", "f1", "用户喜欢红色运动鞋")

    results = store.search("u1", "红色", top_k=10)
    assert results.count("用户喜欢红色运动鞋") == 1


def test_clear_single_user(tmp_path):
    store = _make_store(tmp_path)
    store.index("userA", "f1", "用户喜欢红色运动鞋")
    store.index("userB", "f1", "用户喜欢红色帽子")

    store.clear("userA")

    assert store.search("userA", "红色") == []
    assert "用户喜欢红色帽子" in store.search("userB", "红色")


def test_clear_all_users(tmp_path):
    store = _make_store(tmp_path)
    store.index("userA", "f1", "用户喜欢红色运动鞋")
    store.index("userB", "f1", "用户喜欢红色帽子")

    store.clear()

    assert store.search("userA", "红色") == []
    assert store.search("userB", "红色") == []


def test_fts5_probe_flag_is_bool(tmp_path):
    """探测标志应可读,用于确认实际选用的后端(FTS5 或 LIKE 降级)。"""
    store = _make_store(tmp_path)
    assert isinstance(store.fts5_available, bool)


# ==========================================================================
# H2.2 集成测试:LongTermMemory 接入 MemoryFtsStore
# ==========================================================================

def _three_facts() -> list[MemoryFact]:
    return [
        MemoryFact(content="用户喜欢红色运动鞋", category="preference", created_at="2026-01-01"),
        MemoryFact(content="用户是钻石会员", category="identity", created_at="2026-01-01"),
        MemoryFact(content="用户经常询问物流进度", category="behavior", created_at="2026-01-01"),
    ]


def test_save_syncs_facts_into_fts_index(tmp_path, monkeypatch):
    """1. 写入同步:save() 后可用同一 db 路径的独立 MemoryFtsStore 搜到内容。"""
    monkeypatch.setattr(settings, "memory_fts_enabled", True)
    ltm = LongTermMemory(user_id="u1", memory_dir=str(tmp_path))
    ltm.add_facts(_three_facts())
    ltm.save()

    store = MemoryFtsStore(str(tmp_path / "memory_fts.db"))
    assert "用户喜欢红色运动鞋" in store.search("u1", "红色")
    assert "用户是钻石会员" in store.search("u1", "会员")


def test_recall_hits_and_misses(tmp_path, monkeypatch):
    """2. recall:命中词返回含关键词的 content;不存在的词返回 []。"""
    monkeypatch.setattr(settings, "memory_fts_enabled", True)
    ltm = LongTermMemory(user_id="u1", memory_dir=str(tmp_path))
    ltm.add_facts(_three_facts())
    ltm.save()

    results = ltm.recall("红色")
    assert any("红色" in r for r in results)
    assert ltm.recall("不存在的词") == []


def test_build_prompt_section_hit_first_injection(tmp_path, monkeypatch):
    """3. 命中优先注入:相关段在前、不与其他段重复;无 query 时与旧格式一致。"""
    monkeypatch.setattr(settings, "memory_fts_enabled", True)
    ltm = LongTermMemory(user_id="u1", memory_dir=str(tmp_path))
    ltm.add_facts(_three_facts())
    ltm.save()

    section_no_query = ltm.build_prompt_section()
    section_with_query = ltm.build_prompt_section(query="红色")

    # 无 query:全量单段,旧格式不变
    assert section_no_query is not None
    assert "该用户的历史记忆" in section_no_query
    assert "与当前问题相关的记忆" not in section_no_query

    # 有 query 且命中:相关段在前，其他段在后
    assert section_with_query is not None
    relevant_idx = section_with_query.index("与当前问题相关的记忆")
    other_idx = section_with_query.index("其他历史记忆")
    assert relevant_idx < other_idx

    # 命中的事实不在"其他"段重复出现
    assert section_with_query.count("用户喜欢红色运动鞋") == 1
    # 未命中的事实仍出现在"其他"段
    assert "用户是钻石会员" in section_with_query
    assert "用户经常询问物流进度" in section_with_query


def test_fts_gate_off_falls_back_completely(tmp_path, monkeypatch):
    """4. 门控回退:关闭 memory_fts_enabled 后不建库,有无 query 输出完全一致。"""
    monkeypatch.setattr(settings, "memory_fts_enabled", False)
    ltm = LongTermMemory(user_id="u1", memory_dir=str(tmp_path))
    ltm.add_facts(_three_facts())
    ltm.save()

    assert ltm._fts is None
    assert not (tmp_path / "memory_fts.db").exists()

    section_no_query = ltm.build_prompt_section()
    section_with_query = ltm.build_prompt_section(query="红色")
    assert section_no_query == section_with_query


def test_full_resync_on_save_after_facts_replaced(tmp_path, monkeypatch):
    """5. 策展替换后索引不漂移:facts 整体替换再 save() 后,旧内容不命中、新内容命中。"""
    monkeypatch.setattr(settings, "memory_fts_enabled", True)
    ltm = LongTermMemory(user_id="u1", memory_dir=str(tmp_path))
    ltm.add_facts([
        MemoryFact(content="旧事实红色鞋", category="preference", created_at="2026-01-01"),
    ])
    ltm.save()
    assert "旧事实红色鞋" in ltm.recall("红色")

    ltm.facts = [
        MemoryFact(content="新事实蓝色帽子", category="preference", created_at="2026-01-02"),
    ]
    ltm.save()

    assert ltm.recall("红色") == []
    assert "新事实蓝色帽子" in ltm.recall("蓝色")


def test_reset_clears_fts_index(tmp_path, monkeypatch):
    """6. reset 清索引:reset() 后 search/recall 返回 []。"""
    monkeypatch.setattr(settings, "memory_fts_enabled", True)
    ltm = LongTermMemory(user_id="u1", memory_dir=str(tmp_path))
    ltm.add_facts(_three_facts())
    ltm.save()
    assert ltm.recall("红色") != []

    ltm.reset()
    assert ltm.recall("红色") == []
