"""A/B 门禁的数据可信下限。

**这个洞是走查自进化时推演 + 实测双向确认的。**

A/B 判据只问"有没有比现行版更差",从不问"够不够好"。现行版被基础设施故障
压塌时,`canary_rate < live_rate - max_drop` 变成一个永远不成立的条件——
live=0.03 时它等价于 `canary_rate < -0.07`,于是**任何候选都会自动转正**。

实测的现场:`track-order` 因为 MCP 数据源被代理打断,实战成功率 3%
(success:1 · tool_error:37),而候选池里正好躺着一份它的 `low/canary_ab`
改进候选——低危档是**真 A/B 自动转正**,那份候选本可以在这种基线下无条件上线。
"""

from __future__ import annotations

from app.agent.skills.canary import VARIANT_CANARY, VARIANT_LIVE
from app.agent.skills.risk import AB_SANITY_FLOOR
from app.agent.skills.watchdog import (DECISION_PROMOTE, DECISION_ROLLBACK,
                                       DECISION_WAIT, evaluate_ab)


def _rows(variant: str, n: int, ok: int) -> list[dict]:
    return ([{"variant": variant, "outcome": "success"}] * ok
            + [{"variant": variant, "outcome": "tool_error"}] * (n - ok))


def test_both_arms_broken_does_not_auto_promote():
    """复刻现场:现行 3%、候选 5%,改造前会判 promote。"""
    traces = _rows(VARIANT_LIVE, 40, 1) + _rows(VARIANT_CANARY, 40, 2)
    out = evaluate_ab(traces)
    assert out["decision"] == DECISION_WAIT
    assert "依赖" in out["reason"], "要把人引向依赖排查,而不是含糊地等"


def test_wait_not_rollback_when_both_are_broken():
    """判 wait 而不是 rollback。

    两臂都低说明问题多半不在候选身上(依赖挂了/数据源不对),回滚会把锅扣给
    一份可能没问题的候选,还会掩盖真正的故障。
    """
    traces = _rows(VARIANT_LIVE, 40, 1) + _rows(VARIANT_CANARY, 40, 0)
    assert evaluate_ab(traces)["decision"] != DECISION_ROLLBACK


def test_real_improvement_over_a_broken_baseline_still_promotes():
    """**只在两臂都低时才拦。**

    live 低而 canary 高,恰恰是一次真实的改进(比如候选修好了流程),必须放行——
    否则这道闸会把最有价值的那类迭代拦在门外。
    """
    traces = _rows(VARIANT_LIVE, 40, 2) + _rows(VARIANT_CANARY, 40, 36)
    assert evaluate_ab(traces)["decision"] == DECISION_PROMOTE


def test_healthy_baseline_behaviour_unchanged():
    """基线正常时,判据与改造前逐字节一致。"""
    traces = _rows(VARIANT_LIVE, 40, 36) + _rows(VARIANT_CANARY, 40, 38)
    assert evaluate_ab(traces)["decision"] == DECISION_PROMOTE

    worse = _rows(VARIANT_LIVE, 40, 36) + _rows(VARIANT_CANARY, 40, 20)
    assert evaluate_ab(worse)["decision"] == DECISION_ROLLBACK


def test_floor_is_not_applied_when_only_one_arm_is_low():
    """候选高、现行低 → 放行(上面那条);候选低、现行高 → 仍然回滚。

    后者是这道闸**不该**改变的判定:候选确实把事情做坏了。
    """
    traces = _rows(VARIANT_LIVE, 40, 36) + _rows(VARIANT_CANARY, 40, 4)
    assert evaluate_ab(traces)["decision"] == DECISION_ROLLBACK


def test_floor_is_overridable_for_callers_that_know_better():
    """下限可传参:某些天然低成功率的域(如高难度售后)可以自定。"""
    traces = _rows(VARIANT_LIVE, 40, 8) + _rows(VARIANT_CANARY, 40, 9)
    assert evaluate_ab(traces, sanity_floor=0.0)["decision"] == DECISION_PROMOTE
    assert evaluate_ab(traces, sanity_floor=0.5)["decision"] == DECISION_WAIT


def test_default_floor_comes_from_risk_module():
    """下限只有一处口径,不在 watchdog 里另抄一个数字。"""
    assert 0 < AB_SANITY_FLOOR < 1
    traces = _rows(VARIANT_LIVE, 40, 1) + _rows(VARIANT_CANARY, 40, 1)
    assert f"{AB_SANITY_FLOOR}" in evaluate_ab(traces)["reason"]


def test_sample_guards_still_come_first():
    """样本量不足的判定优先于下限判定——否则"样本太少"会被误报成"依赖坏了"。"""
    traces = _rows(VARIANT_LIVE, 40, 1) + _rows(VARIANT_CANARY, 3, 0)
    out = evaluate_ab(traces)
    assert out["decision"] == DECISION_WAIT
    assert "灰度样本不足" in out["reason"]
