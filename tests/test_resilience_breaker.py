from app.resilience.breaker import CircuitBreaker


def test_trips_after_threshold():
    clock = {"t": 100.0}
    b = CircuitBreaker(threshold=3, cooldown=60, now=lambda: clock["t"])
    assert b.allow() is True
    b.record_failure(); b.record_failure()
    assert b.allow() is True          # 2 次未到阈值
    b.record_failure()                # 第 3 次 → 跳闸
    assert b.allow() is False
    assert b.is_open is True


def test_cooldown_half_open():
    clock = {"t": 100.0}
    b = CircuitBreaker(threshold=2, cooldown=60, now=lambda: clock["t"])
    b.record_failure(); b.record_failure()
    assert b.allow() is False
    clock["t"] = 100.0 + 61            # 冷却结束
    assert b.allow() is True           # 半开放探测


def test_success_resets():
    clock = {"t": 100.0}
    b = CircuitBreaker(threshold=2, cooldown=60, now=lambda: clock["t"])
    b.record_failure()
    b.record_success()
    b.record_failure()
    assert b.allow() is True           # 计数被 success 清零,单次失败不跳闸
