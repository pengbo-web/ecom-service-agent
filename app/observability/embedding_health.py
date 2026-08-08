"""embedding 调用失败的可见性兜底:计数 + 最近错误摘要。

背景(W1 L1):FAQ 语义缓存/KB 预召回把 embedding 异常当"未命中/无结果"
吞掉是刻意的 fail-soft——单次调用失败不该打断买家这一轮对话。但"吞掉"
不能等于"没发生过":过去这一类失败没有任何计数、任何埋点,导致端点换了
模型返回 404 之后，整个子系统静默死了很久也没人发现。

本模块只做两件轻量的事，不做告警/熔断动作（那属于上层策略）：
①线程安全累计计数（按来源分组，如 faq_cache / kb_recall / kb_search）
②保留最近一次失败的摘要，供管理端/排障脚本读取。

与 CostGuard/CircuitBreaker 同样的姿态：一个小的、带锁的、可测试的类 +
一个进程级单例。调用方在各自的 fail-soft except 块里调一次 `record_embedding_
failure(source, error)` 即可，不影响主流程、不抛异常。
"""

import threading
import time


class EmbeddingFailureTracker:
    """线程安全的 embedding 失败计数器 + 最近错误摘要。"""

    def __init__(self, now=time.time):
        self._now = now
        self._lock = threading.Lock()
        self._total = 0
        self._by_source: dict[str, int] = {}
        self._last_error: str | None = None
        self._last_source: str | None = None
        self._last_at: float | None = None

    def record(self, source: str, error: BaseException) -> None:
        """记一次失败;source 标识调用点(如 faq_cache/kb_recall/kb_search)。"""
        with self._lock:
            self._total += 1
            self._by_source[source] = self._by_source.get(source, 0) + 1
            self._last_error = str(error)[:500]
            self._last_source = source
            self._last_at = self._now()

    def snapshot(self) -> dict:
        """当前累计计数 + 最近一次失败摘要,只读快照,供管理端/排障读取。"""
        with self._lock:
            return {
                "total": self._total,
                "by_source": dict(self._by_source),
                "last_error": self._last_error,
                "last_source": self._last_source,
                "last_at": self._last_at,
            }

    def reset(self) -> None:
        """仅供测试用:清空计数器,避免用例间串状态。"""
        with self._lock:
            self._total = 0
            self._by_source = {}
            self._last_error = None
            self._last_source = None
            self._last_at = None


_tracker = EmbeddingFailureTracker()


def record_embedding_failure(source: str, error: BaseException) -> None:
    """记一次 embedding 调用失败(进程级单例计数器,fail-soft 调用点用)。"""
    _tracker.record(source, error)


def embedding_failure_stats() -> dict:
    """读取当前累计的 embedding 失败统计(供管理端/排障脚本调用)。"""
    return _tracker.snapshot()


def reset_embedding_failure_stats() -> None:
    """仅供测试用:清空计数器。"""
    _tracker.reset()
