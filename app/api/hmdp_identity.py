"""会话用户 ↔ hmdp userId 解析。

hmdp 登录后把用户存进 Redis Hash `login:token:{token}`(字段 id/nickName/icon)。
客服 agent 拿到前端传来的同一个 hmdp token,读同一 Redis 反查 id,即得 hmdp userId。
用于把"客服会话用户"绑定到 hmdp 身份(order.user_id 归属校验的同一命名空间)。
"""

from __future__ import annotations

from typing import Optional

_LOGIN_TOKEN_PREFIX = "login:token:"
_redis = None
_breaker = None


def _default_redis():
    global _redis
    if _redis is None:
        from app.config.settings import settings
        from app.session.redis_health import make_client
        # hmdp 的 Redis 在 127.0.0.1:6379。localhost→127.0.0.1 的归一(Windows 下
        # localhost 先解析成 IPv6 ::1 而 Redis 只监听 IPv4)与强制短超时都下沉进
        # make_client 了——原先只有本模块做了这两件事,另外三处 Redis 消费方都没有。
        url = (getattr(settings, "redis_url", None) or "redis://127.0.0.1:6379")
        _redis = make_client(url)
    return _redis


def _default_breaker():
    """失败后退避。**这一处原先是漏的**,而它比另外几处更疼:
    `resolve_hmdp_user` 在 `/api/chat` 里是**每个请求必经**的一步(见
    app/api/app.py 里 fast-path 之前那次调用),所以 Redis 一挂,每一轮对话
    都要固定多付一次 socket 超时——实测就是那条"零 LLM 的固定话术也要 2 秒"。

    fail-soft 只保证不崩,不保证不慢;这条是把"不慢"也补上。
    """
    global _breaker
    if _breaker is None:
        from app.config.settings import settings
        from app.session.redis_health import Breaker
        _breaker = Breaker(
            float(getattr(settings, "session_store_redis_retry_cooldown_s", 5.0)),
            name="hmdp 身份解析", dependency="Redis")
    return _breaker


def resolve_hmdp_user(token: Optional[str], redis_client=None) -> Optional[str]:
    """据 hmdp token 反查 hmdp userId(字符串);token 无效/未登录返回 None。

    Redis 不可用时返回 None(与"token 无效"同一出口——调用方本来就要处理 None,
    不需要为此多一条分支),并开冷却窗口:冷却期内直接返回 None,不再连 Redis。

    注意冷却期内的语义**与故障时完全一致**——都是"解析不出 hmdp 身份"。所以这
    不是拿正确性换延迟:该拿不到的照样拿不到,只是不再为每个请求重付一次超时。
    """
    if not token:
        return None
    # 显式传了 client 的调用方(测试/内部)不走全局冷却:它们的 client 与全局
    # 那个不是同一个连接,凭全局故障判定去跳过它们是错的。
    if redis_client is None and _default_breaker().open:
        return None
    try:
        r = redis_client or _default_redis()
        val = r.hget(_LOGIN_TOKEN_PREFIX + token, "id")
    except Exception as exc:  # noqa: BLE001 身份解析失败不该打死买家链路
        if redis_client is None:
            _default_breaker().record_failure("resolve", exc)
        return None
    if redis_client is None:
        _default_breaker().record_success()
    if val is None:
        return None
    return val.decode() if isinstance(val, (bytes, bytearray)) else str(val)


def seed_demo_hmdp_identity(token, user_id, nickname="演示用户", icon="",
                            redis_client=None) -> bool:
    """把 demo hmdp 身份写入 Redis(login:token:{token} = {id,nickName,icon})。

    效果:①该 token 成为 hmdp 后端认可的有效登录会话(hmdp LoginInterceptor 读同一 key);
    ②supply resolve_hmdp_user 反查 → demo userId。用于 DEMO_MODE 下开箱聊真实订单,
    零登录零验证码。幂等,失败静默(不阻断启动)。
    """
    if not token or not user_id:
        return False
    try:
        r = redis_client or _default_redis()
        r.hset(_LOGIN_TOKEN_PREFIX + token,
               mapping={"id": str(user_id), "nickName": nickname, "icon": icon})
        return True
    except Exception:
        return False
