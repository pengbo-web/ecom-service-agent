"""写操作幂等层(R6):防重复副作用。

给写工具(退款/取消/改地址/成交)执行前算幂等键 = hash(session_id, tool, args);
若 `applied:{key}` 已有(上次成功结果)→ 直接返回缓存,不再执行副作用。
只缓存 success 结果;need_confirm / 失败不缓存(允许后续确认/重试)。

作用:任何写操作被**重试/重放/续跑**时都不会重复执行——这是"可安全续跑"的基石。
仅 redis 后端启用(多实例共享才有意义);file/测试默认 Null(不改行为)。
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional


def idempotency_key(session_id: Optional[str], tool: str, args: dict) -> str:
    payload = json.dumps(
        {"s": session_id or "", "t": tool, "a": args or {}},
        sort_keys=True, ensure_ascii=False,
    )
    return "applied:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()


class NullIdempotencyStore:
    """不去重(默认)。"""
    def get(self, key: str) -> Optional[str]:
        return None

    def put(self, key: str, result: str) -> None:
        return None


class RedisIdempotencyStore:
    """Redis `applied:{key}` 缓存已成功执行的写结果,带 TTL。"""
    def __init__(self, client, ttl: int = 600):
        self._r = client
        self._ttl = ttl

    def get(self, key: str) -> Optional[str]:
        raw = self._r.get(key)
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else raw

    def put(self, key: str, result: str) -> None:
        self._r.set(key, result, ex=self._ttl)


_store = None


def get_idempotency_store():
    global _store
    if _store is None:
        _store = _build_from_settings()
    return _store


def set_idempotency_store(store) -> None:
    global _store
    _store = store


def _build_from_settings():
    from app.config.settings import settings
    if (getattr(settings, "idempotency_enabled", True)
            and getattr(settings, "session_store_backend", "file") == "redis"):
        import redis
        return RedisIdempotencyStore(redis.from_url(settings.redis_url),
                                     ttl=settings.idempotency_ttl)
    return NullIdempotencyStore()
