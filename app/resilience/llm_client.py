"""ResilientChatClient：透明代理包装主/备 OpenAI 客户端，做错误分类重试 + 主备切换 + 熔断。

用法与原生 client 一致（`.chat.completions.create(**kw)` / `.beta.chat.completions.parse(**kw)`），
调用方无需改动;`model` 会被本代理按主/备各自配置覆盖。其余属性透传给主 client。
不改核心 chat.py 逻辑,仅在构造点替换 client（见 factory.make_resilient_client）。
"""

import time

from app.resilience.breaker import CircuitBreaker
from app.resilience.errors import classify_error, retry_after_seconds


class ResilientChatClient:
    def __init__(self, primary, secondary, primary_model, secondary_model=None,
                 breaker: CircuitBreaker | None = None, max_retries: int = 2,
                 backoff=(1, 2, 4), retry_after_cap: float = 30.0, sleep=time.sleep):
        self._primary = primary
        self._secondary = secondary
        self._primary_model = primary_model
        self._secondary_model = secondary_model or primary_model
        self._breaker = breaker or CircuitBreaker()
        self._max_retries = max_retries
        self._backoff = backoff
        self._retry_after_cap = retry_after_cap
        self._sleep = sleep
        self.chat = _Chat(self)
        self.beta = _Beta(self)

    # 统计（可上报看板）
    stats: dict = {}

    def _delay(self, attempt: int, exc: Exception) -> float:
        ra = retry_after_seconds(exc)
        base = self._backoff[min(attempt, len(self._backoff) - 1)]
        return min(ra if ra is not None else base, self._retry_after_cap)

    def _run(self, invoke):
        """invoke(client, model) -> response;封装重试/切换/熔断。"""
        has_secondary = self._secondary is not None
        last: Exception | None = None

        # ---- 主模型阶段 ----
        if not has_secondary or self._breaker.allow():
            for attempt in range(self._max_retries + 1):
                try:
                    resp = invoke(self._primary, self._primary_model)
                    self._breaker.record_success()
                    return resp
                except Exception as e:  # noqa: BLE001
                    last = e
                    cls = classify_error(e)
                    self._breaker.record_failure()
                    if cls == "fatal":
                        raise
                    if cls == "switch":
                        break                      # 换模型有望,不再重试主
                    if attempt < self._max_retries:
                        self._sleep(self._delay(attempt, e))
                        continue
                    break                          # 瞬时错误重试耗尽 → 走备用

        # ---- 备用模型阶段 ----
        if has_secondary:
            try:
                return invoke(self._secondary, self._secondary_model)
            except Exception as e:  # noqa: BLE001
                last = e
                if classify_error(e) == "fatal":
                    raise

        if last is not None:
            raise last
        raise RuntimeError("ResilientChatClient: 无可用尝试")

    def __getattr__(self, name):
        return getattr(self._primary, name)


class _Completions:
    def __init__(self, rc: ResilientChatClient):
        self._rc = rc

    def create(self, **kw):
        return self._rc._run(lambda c, m: c.chat.completions.create(**{**kw, "model": m}))


class _Chat:
    def __init__(self, rc: ResilientChatClient):
        self._rc = rc
        self.completions = _Completions(rc)


class _BetaCompletions:
    def __init__(self, rc: ResilientChatClient):
        self._rc = rc

    def parse(self, **kw):
        return self._rc._run(lambda c, m: c.beta.chat.completions.parse(**{**kw, "model": m}))


class _BetaChat:
    def __init__(self, rc: ResilientChatClient):
        self.completions = _BetaCompletions(rc)


class _Beta:
    def __init__(self, rc: ResilientChatClient):
        self.chat = _BetaChat(rc)
