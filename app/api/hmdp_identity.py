"""会话用户 ↔ hmdp userId 解析。

hmdp 登录后把用户存进 Redis Hash `login:token:{token}`(字段 id/nickName/icon)。
客服 agent 拿到前端传来的同一个 hmdp token,读同一 Redis 反查 id,即得 hmdp userId。
用于把"客服会话用户"绑定到 hmdp 身份(order.user_id 归属校验的同一命名空间)。
"""

from __future__ import annotations

from typing import Optional

_LOGIN_TOKEN_PREFIX = "login:token:"
_redis = None


def _default_redis():
    global _redis
    if _redis is None:
        import redis
        from app.config.settings import settings
        url = getattr(settings, "redis_url", None) or "redis://127.0.0.1:6379"
        _redis = redis.from_url(url)
    return _redis


def resolve_hmdp_user(token: Optional[str], redis_client=None) -> Optional[str]:
    """据 hmdp token 反查 hmdp userId(字符串);token 无效/未登录返回 None。"""
    if not token:
        return None
    try:
        r = redis_client or _default_redis()
        val = r.hget(_LOGIN_TOKEN_PREFIX + token, "id")
    except Exception:
        return None
    if val is None:
        return None
    return val.decode() if isinstance(val, (bytes, bytearray)) else str(val)
