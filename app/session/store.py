"""会话存储抽象:热会话上下文的持久化介质可插拔。

- **key**:统一用会话的 session_path(agent 本就持有的字符串)。
  - FileSessionStore 把它当文件路径(与原 storage 行为完全一致)。
  - RedisSessionStore 取其 stem 作 session id → `sess:{id}`(即 SessionManager 的 session_id)。
- **SessionState**:{version, messages, summary, short_term_memory[, status, step_seq, pending]}。
  R1 只落前四项;status/step_seq/pending 由后续 checkpoint 阶段(R2/R3)填充。

后端由 settings.session_store_backend 决定(file | redis);测试可用 fakeredis 注入 RedisSessionStore,
或用 set_session_store 直接替换全局实例(与 get_db/set_db 一致的模式)。

R1.x 容灾降级:Redis 连接失败/超时时 RedisSessionStore 不再向上抛异常,而是本次
操作原地回退到本地文件后端(见 RedisSessionStore 类文档的退避设计说明)。这是
线上真实事故复盘后补的——Redis 单点故障此前会让 /api/session/{id}/history 等
读会话接口直接 500,买家整个聊天不可用。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional, Protocol

from app.agent.storage import delete_session, load_session, save_session

logger = logging.getLogger(__name__)

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

    容灾降级(Redis 连不上/超时):
    - **按次判断,不永久切换**——每次 load/save/delete 都会先看"冷却窗口"是否已过期
      (`_redis_available`),没过期就直接走本地文件兜底,过期了就照常尝试连 Redis。
      这样 Redis 恢复后下一次调用自然就切回去,不存在"一旦降级就再也不回去"的死锁态。
    - **退避用一个时间戳,不是后台轮询/看门狗线程**——连接失败时把 `_down_until` 设为
      "现在 + 冷却秒数",之后的调用只比较时间戳(一次浮点比较,几乎零成本),冷却期内
      不会再对已经挂掉的 Redis 发起一次连接(每次连接失败本身都要付一次 TCP connect
      超时,冷却窗口就是为了不让这个超时叠加到买家每一轮对话上)。冷却时长由
      `settings.session_store_redis_retry_cooldown_s` 配置,默认几秒——足够躲开
      "同一次故障连续被摞很多次超时",又不会让恢复后的切回延迟太久。
    - **数据错误不受影响**——JSON 解析失败(损坏的 payload)仍然只在 load() 里按原逻辑
      返回 None,不会被当成连接问题触发降级,也不会被回退文件路径掩盖掉。
    """

    # 连接失败后的默认冷却时长(秒);可通过构造参数或 settings 覆盖。
    DEFAULT_RETRY_COOLDOWN_S = 5.0

    def __init__(
        self,
        client,
        ttl: int = 3600,
        fallback: Optional["FileSessionStore"] = None,
        retry_cooldown_s: Optional[float] = None,
        clock=time.monotonic,
    ):
        self._r = client
        self._ttl = ttl
        # 降级兜底后端:文件存储,行为与 FileSessionStore 完全一致(不重复实现)。
        self._fallback = fallback if fallback is not None else FileSessionStore()
        self._retry_cooldown_s = (
            retry_cooldown_s if retry_cooldown_s is not None else self.DEFAULT_RETRY_COOLDOWN_S
        )
        # 可注入的时钟(测试用假时钟推进时间,不必真的 sleep);生产用单调时钟,
        # 不受系统时间被人为/NTP 调整影响。
        self._clock = clock
        # <= 当前时刻即视为"可以尝试连 Redis";初始为 -1,永远小于任何 clock() 取值。
        self._down_until: float = -1.0
        # redis 的连接类异常惰性拿一次:只有真正构造 RedisSessionStore(意味着
        # redis 包必然已安装,client 就是拿它构造出来的)才 import,file 后端
        # 场景仍然零 redis 依赖,与模块工厂 _build_from_settings 的既有设计一致。
        import redis.exceptions as _redis_exceptions
        self._conn_errors: tuple = (
            _redis_exceptions.ConnectionError,
            _redis_exceptions.TimeoutError,
        )

    def _redis_available(self) -> bool:
        return self._clock() >= self._down_until

    def _mark_down(self, key: str, op: str, exc: Exception) -> None:
        """记一次连接失败:开冷却窗口 + 大声警告(日志 + 观测事件),绝不静默。"""
        self._down_until = self._clock() + self._retry_cooldown_s
        logger.warning(
            "会话存储降级:Redis 连接失败(%s: %s),op=%s key=%s 已回退本地文件存储处理"
            "本次请求;%.1fs 冷却期内不再尝试连接 Redis(避免每次调用都重复承担一次连接"
            "超时),到期后自动重试、Redis 恢复即自动切回,不会永久停留在降级态。"
            "【运维须知】降级期间会话只落在本机文件系统,多实例部署下不再跨实例共享"
            "会话状态——其它实例看不到这些变更,请尽快恢复 Redis,不要把这条当噪音忽略。",
            type(exc).__name__, exc, op, key, self._retry_cooldown_s,
            extra={
                "event": "session_store_degraded",
                "backend": "redis",
                "op": op,
                "key": key,
                "cooldown_s": self._retry_cooldown_s,
                "error_type": type(exc).__name__,
            },
        )

    @staticmethod
    def _redis_key(key: str) -> str:
        return f"sess:{Path(key).stem}"

    def load(self, key: str) -> Optional[SessionState]:
        if self._redis_available():
            try:
                raw = self._r.get(self._redis_key(key))
            except self._conn_errors as e:
                self._mark_down(key, "load", e)
                return self._fallback.load(key)
        else:
            return self._fallback.load(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def save(self, key: str, state: SessionState) -> None:
        if self._redis_available():
            try:
                self._r.set(
                    self._redis_key(key),
                    json.dumps(state, ensure_ascii=False),
                    ex=self._ttl,
                )
                return
            except self._conn_errors as e:
                self._mark_down(key, "save", e)
        self._fallback.save(key, state)

    def delete(self, key: str) -> None:
        if self._redis_available():
            try:
                self._r.delete(self._redis_key(key))
                return
            except self._conn_errors as e:
                self._mark_down(key, "delete", e)
        self._fallback.delete(key)


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
        cooldown = getattr(
            settings, "session_store_redis_retry_cooldown_s",
            RedisSessionStore.DEFAULT_RETRY_COOLDOWN_S,
        )
        # 经 make_client 而不是直接 redis.from_url:后者不传超时时 redis-py 默认
        # 是 None(无限阻塞),而这条路在买家回复的热路径上。见 redis_health.py。
        from app.session.redis_health import make_client
        return RedisSessionStore(
            make_client(settings.redis_url), settings.session_ttl,
            retry_cooldown_s=cooldown,
        )
    return FileSessionStore()
