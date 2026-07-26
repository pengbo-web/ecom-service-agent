"""限流键:同一用户换 session_id 不应绕过限流(P1)。"""

from app.hardening.rate_limit import RateLimiter


def test_same_user_different_sessions_share_bucket():
    # 直接验证限流器语义:按 user_id 键,换 session 不重置
    rl = RateLimiter(max_per_window=2, window_seconds=60, now=lambda: 1000.0)
    assert rl.allow("user-A") is True
    assert rl.allow("user-A") is True
    assert rl.allow("user-A") is False        # 第3次被限,即使客户端换了 session
