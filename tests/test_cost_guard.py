from app.hardening.cost_guard import CostGuard


def test_allows_until_budget():
    cg = CostGuard(max_requests_per_day=2, now=lambda: 100.0)
    assert cg.allow() is True
    assert cg.allow() is True
    assert cg.allow() is False
    assert cg.spent() == 2


def test_resets_next_day():
    clock = {"t": 100.0}
    cg = CostGuard(max_requests_per_day=1, now=lambda: clock["t"])
    assert cg.allow() is True
    assert cg.allow() is False
    clock["t"] = 100.0 + 86400        # 第二天
    assert cg.allow() is True
    assert cg.spent() == 1
