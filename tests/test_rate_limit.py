from app.hardening.rate_limit import RateLimiter


def test_allows_within_limit():
    rl = RateLimiter(max_per_window=3, window_seconds=60, now=lambda: 1000.0)
    assert rl.allow("s1") is True
    assert rl.allow("s1") is True
    assert rl.allow("s1") is True
    assert rl.allow("s1") is False   # 第4次超限


def test_window_slides():
    clock = {"t": 1000.0}
    rl = RateLimiter(max_per_window=2, window_seconds=60, now=lambda: clock["t"])
    assert rl.allow("s1") is True
    assert rl.allow("s1") is True
    assert rl.allow("s1") is False
    clock["t"] = 1000.0 + 61          # 窗口滑过
    assert rl.allow("s1") is True


def test_keys_isolated():
    rl = RateLimiter(max_per_window=1, window_seconds=60, now=lambda: 1000.0)
    assert rl.allow("s1") is True
    assert rl.allow("s2") is True     # 不同会话独立
    assert rl.allow("s1") is False
