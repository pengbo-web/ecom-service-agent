"""看门狗:按实战轨迹判定灰度候选该转正、该回滚还是继续观察。纯函数,不碰 DB。"""

from app.agent.skills.canary import VARIANT_CANARY, VARIANT_LIVE
from app.agent.skills.watchdog import (
    DECISION_PROMOTE,
    DECISION_ROLLBACK,
    DECISION_WAIT,
    evaluate_ab,
    evaluate_absolute,
    success_rate,
)


def _rows(variant, n_success, n_fail):
    return ([{"variant": variant, "outcome": "success"}] * n_success
            + [{"variant": variant, "outcome": "handoff"}] * n_fail)


# ---------- success_rate ----------

def test_success_rate_basic():
    assert success_rate(_rows(VARIANT_LIVE, 3, 1)) == 0.75


def test_success_rate_empty_is_none():
    assert success_rate([]) is None


# ---------- evaluate_ab ----------

def test_ab_waits_until_enough_canary_samples():
    traces = _rows(VARIANT_LIVE, 50, 0) + _rows(VARIANT_CANARY, 3, 0)
    result = evaluate_ab(traces, min_samples=10)
    assert result["decision"] == DECISION_WAIT
    assert "样本不足" in result["reason"]
    assert result["canary_samples"] == 3


def test_ab_promotes_when_candidate_not_worse():
    traces = _rows(VARIANT_LIVE, 6, 4) + _rows(VARIANT_CANARY, 9, 1)
    result = evaluate_ab(traces, min_samples=10, max_drop=0.1)
    assert result["decision"] == DECISION_PROMOTE
    assert result["live_rate"] == 0.6
    assert result["canary_rate"] == 0.9


def test_ab_rolls_back_when_candidate_worse_than_tolerance():
    traces = _rows(VARIANT_LIVE, 9, 1) + _rows(VARIANT_CANARY, 4, 6)
    result = evaluate_ab(traces, min_samples=10, max_drop=0.1)
    assert result["decision"] == DECISION_ROLLBACK
    assert "低于" in result["reason"]


def test_ab_tolerates_small_dip():
    traces = _rows(VARIANT_LIVE, 10, 0) + _rows(VARIANT_CANARY, 19, 1)
    result = evaluate_ab(traces, min_samples=10, max_drop=0.1)
    assert result["decision"] == DECISION_PROMOTE   # 掉 0.05 < 容差 0.1


def test_ab_waits_without_live_control_group():
    """没有对照组不能用 A/B 判(该走绝对值模式)。"""
    traces = _rows(VARIANT_CANARY, 20, 0)
    result = evaluate_ab(traces, min_samples=10)
    assert result["decision"] == DECISION_WAIT
    assert "对照" in result["reason"]


def test_ab_treats_missing_variant_as_live():
    """Task 13 迁移前的老轨迹没有 variant 字段,按 live 计。"""
    traces = [{"outcome": "success"}] * 10 + _rows(VARIANT_CANARY, 10, 0)
    result = evaluate_ab(traces, min_samples=10)
    assert result["live_samples"] == 10
    assert result["decision"] == DECISION_PROMOTE


def test_ab_waits_when_live_control_arm_too_small():
    """对照臂样本不足也必须 wait:1 条恰好失败的 live 会让烂候选看起来"不劣化"。"""
    traces = _rows(VARIANT_LIVE, 0, 1) + _rows(VARIANT_CANARY, 3, 7)
    result = evaluate_ab(traces, min_samples=10)
    assert result["decision"] == DECISION_WAIT
    assert "对照样本不足" in result["reason"]


# ---------- evaluate_absolute ----------

def test_absolute_waits_until_enough_samples():
    result = evaluate_absolute(_rows(VARIANT_LIVE, 5, 0), min_samples=30)
    assert result["decision"] == DECISION_WAIT


def test_absolute_promotes_when_rate_above_floor():
    result = evaluate_absolute(_rows(VARIANT_LIVE, 25, 5), min_samples=30, min_rate=0.6)
    assert result["decision"] == DECISION_PROMOTE


def test_absolute_rolls_back_when_rate_below_floor():
    result = evaluate_absolute(_rows(VARIANT_LIVE, 9, 21), min_samples=30, min_rate=0.6)
    assert result["decision"] == DECISION_ROLLBACK
    assert "低于下限" in result["reason"]
