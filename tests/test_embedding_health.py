"""embedding 失败计数器(W1 L1):线程安全累计 + 最近错误摘要。"""

import threading

from app.observability.embedding_health import (
    EmbeddingFailureTracker,
    embedding_failure_stats,
    record_embedding_failure,
    reset_embedding_failure_stats,
)


def test_record_increments_total_and_by_source():
    reset_embedding_failure_stats()
    record_embedding_failure("faq_cache", RuntimeError("404 model_not_found"))
    record_embedding_failure("kb_recall", RuntimeError("timeout"))
    record_embedding_failure("faq_cache", RuntimeError("again"))
    stats = embedding_failure_stats()
    assert stats["total"] == 3
    assert stats["by_source"] == {"faq_cache": 2, "kb_recall": 1}
    assert stats["last_source"] == "faq_cache"
    assert "again" in stats["last_error"]
    reset_embedding_failure_stats()


def test_reset_clears_everything():
    record_embedding_failure("faq_cache", RuntimeError("x"))
    reset_embedding_failure_stats()
    stats = embedding_failure_stats()
    assert stats == {"total": 0, "by_source": {}, "last_error": None,
                     "last_source": None, "last_at": None}


def test_thread_safety_under_concurrent_writes():
    tracker = EmbeddingFailureTracker()

    def _hammer():
        for _ in range(200):
            tracker.record("kb_recall", RuntimeError("boom"))

    threads = [threading.Thread(target=_hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert tracker.snapshot()["total"] == 1600   # 8 * 200,无锁的话几乎不可能刚好等于这个数
