from app.agent.tools.bargain import compute_offer


def test_explicit_floor_ladder_round0():
    # P=1000, F=800, rounds=0 → ladder = 800 + 200*0.5 = 900
    r = compute_offer(1000.0, 800.0, buyer_offer=None, rounds=0)
    assert r["decision"] == "counter"
    assert r["suggested_price"] == 900.0
    assert r["floor"] == 800.0
    assert r["floor_hit"] is False


def test_accept_when_offer_at_or_above_ladder():
    # buyer 950 ≥ ladder 900 且 ≥ F → accept 950
    r = compute_offer(1000.0, 800.0, buyer_offer=950.0, rounds=0)
    assert r["decision"] == "accept"
    assert r["suggested_price"] == 950.0


def test_counter_when_offer_between_floor_and_ladder():
    # buyer 820 ≥ F(800) 但 < ladder(900) → counter 900
    r = compute_offer(1000.0, 800.0, buyer_offer=820.0, rounds=0)
    assert r["decision"] == "counter"
    assert r["suggested_price"] == 900.0


def test_reject_below_floor():
    r = compute_offer(1000.0, 800.0, buyer_offer=700.0, rounds=0)
    assert r["decision"] == "reject"
    assert r["suggested_price"] == 800.0
    assert r["floor_hit"] is True


def test_accept_when_offer_above_list_price():
    r = compute_offer(1000.0, 800.0, buyer_offer=1200.0, rounds=0)
    assert r["decision"] == "accept"
    assert r["suggested_price"] == 1000.0


def test_ladder_decreases_with_rounds():
    r0 = compute_offer(1000.0, 800.0, None, 0)["suggested_price"]  # 900
    r1 = compute_offer(1000.0, 800.0, None, 1)["suggested_price"]  # 850
    r2 = compute_offer(1000.0, 800.0, None, 2)["suggested_price"]  # 825
    assert r0 == 900.0 and r1 == 850.0 and r2 == 825.0
    assert r0 > r1 > r2 > 800.0


def test_after_max_rounds_hits_floor():
    r = compute_offer(1000.0, 800.0, None, 5)
    assert r["suggested_price"] == 800.0
    assert r["floor_hit"] is True


def test_floor_ratio_fallback_when_no_explicit_floor():
    # floor_price=None → F = 1000*0.85 = 850；ladder0 = 850 + 150*0.5 = 925
    r = compute_offer(1000.0, None, buyer_offer=None, rounds=0)
    assert r["floor"] == 850.0
    assert r["suggested_price"] == 925.0


def test_suggested_never_below_floor():
    for rounds in range(0, 8):
        r = compute_offer(1000.0, 800.0, buyer_offer=1.0, rounds=rounds)
        assert r["suggested_price"] >= 800.0
