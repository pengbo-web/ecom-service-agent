"""MemoryFtsStore 测试:验证中文关键词召回、user_id 隔离、top_k、去重。"""

from __future__ import annotations

from app.agent.memory.fts_store import MemoryFtsStore


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
