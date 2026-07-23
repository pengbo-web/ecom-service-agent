"""会话存储抽象:热会话上下文的持久化介质可插拔。

- **key**:统一用会话的 session_path(agent 本就持有的字符串)。
  - FileSessionStore 把它当文件路径(与原 storage 行为完全一致)。
  - RedisSessionStore 取其 stem 作 session id → `sess:{id}`(即 SessionManager 的 session_id)。
- **SessionState**:{version, messages, summary, short_term_memory[, status, step_seq, pending]}。
  R1 只落前四项;status/step_seq/pending 由后续 checkpoint 阶段(R2/R3)填充。

后端由 settings.session_store_backend 决定(file | redis);测试可用 fakeredis 注入 RedisSessionStore,
或用 set_session_store 直接替换全局实例(与 get_db/set_db 一致的模式)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Protocol

from app.agent.storage import delete_session, load_session, save_session

SessionState = dict


class SessionStore(Protocol):
    def load(self, key: str) -> Optional[SessionState]: ...
    def save(self, key: str, state: SessionState) -> None: ...
    def delete(self, key: str) -> None: ...


class FileSessionStore:
    """本地文件后端:key 即文件路径,复用原子写的 storage(行为与改造前一致)。"""

    def load(self, key: str) -> Optional[SessionState]:
        return load_session(key)

    def save(self, key: str, state: SessionState) -> None:
        save_session(
            key,
            state.get("messages", []),
            state.get("summary"),
            short_term_memory=state.get("short_term_memory"),
        )

    def delete(self, key: str) -> None:
        delete_session(key)


class RedisSessionStore:
    """Redis 后端:热会话 `sess:{session_id}` 存整块 JSON,带 TTL(每次 save 续期)。

    client 可注入(生产传 redis.from_url(...),测试传 fakeredis),便于单测不触网。
    """

    def __init__(self, client, ttl: int = 3600):
        self._r = client
        self._ttl = ttl

    @staticmethod
    def _redis_key(key: str) -> str:
        return f"sess:{Path(key).stem}"

    def load(self, key: str) -> Optional[SessionState]:
        raw = self._r.get(self._redis_key(key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def save(self, key: str, state: SessionState) -> None:
        self._r.set(
            self._redis_key(key),
            json.dumps(state, ensure_ascii=False),
            ex=self._ttl,
        )

    def delete(self, key: str) -> None:
        self._r.delete(self._redis_key(key))


# ---- 全局单例 + 工厂(与 db 的 get_db/set_db 一致)----
_store: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    global _store
    if _store is None:
        _store = _build_from_settings()
    return _store


def set_session_store(store: Optional[SessionStore]) -> None:
    """测试/运行时替换全局 store(传 None 复位,下次按 settings 重建)。"""
    global _store
    _store = store


def _build_from_settings() -> SessionStore:
    from app.config.settings import settings
    backend = getattr(settings, "session_store_backend", "file")
    if backend == "redis":
        import redis  # 延迟导入,file 后端无需 redis 依赖
        return RedisSessionStore(redis.from_url(settings.redis_url), settings.session_ttl)
    return FileSessionStore()
