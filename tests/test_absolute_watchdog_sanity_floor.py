"""极低的成功率不该触发回滚——它多半不是关于这份 skill 的陈述。

**实测缺陷**(走查 Skill 自进化时,从页面上那行 "track-order 实战成功率 27%
(success:14 · tool_error:37)" 顺下来抓到)。

`evaluate_ab` 已经拒绝在"两臂都极低"的基线上做判定,理由写在它自己的 docstring 里:

> 两臂都低说明**问题多半不在候选身上**(依赖挂了、数据源不对),回滚会把锅扣给一份
> 可能没问题的候选,还会掩盖真正的故障。正确的动作是停下来去查依赖。

而 `evaluate_absolute` 是六行、一句 docstring、**没有这个保护**:直接
`rate < min_rate → ROLLBACK`。同一个依赖故障,在 A/B 那条路上被正确地拦成 wait,
在绝对值这条路上会把一份正常的 skill 回滚掉,理由写成"成功率 0.27 低于下限 0.6"
——读起来是 skill 质量问题。

**那 0.27 是怎么来的**(51 条真实轨迹,逐条查过):

1. **36 条来自合成用户**(`ab*` 压测 / `ev*` 评测 / `trk*`),只有 15 条来自真实
   买家 `1`。而页面把这个数叫"**实战**成功率"。
2. **36/37 条失败是同一个订单号 `ORD-20240115-001`** —— 评测数据集 `order_query_basic`
   里那个。它在这个库里本来就不存在。
3. **37 条 tool_error 里有 11 条其实兜底成功了**(失败之后的调用成功)。而
   track-order 的技能描述里明写着"支持订单号不存在时的友好兜底与自助确认机制"
   ——**它正因为有兜底而被扣分**,兜底覆盖得越好分数越低。

这三条没有一条是"这份 skill 写坏了"。回滚不可白做:它会把线上换成上一版并结束灰度。

**没修的部分**:第 3 条要动 `outcome()` 的语义(兜底成功既不是 success 也不是
tool_error),会波及回滚判定、异常扫描的 tool_error_rate、自改进样本筛选和两处界面
——改一半比不改更糟。第 1 条要给轨迹加来源标记(现有 `variant` 只分 live/canary,
分不出真实买家与压测),且**无法追溯修复**历史数据。两条都已另开任务。
"""

import pytest

from app.agent.skills.risk import AB_SANITY_FLOOR
from app.agent.skills.watchdog import (DECISION_PROMOTE, DECISION_ROLLBACK,
                                       DECISION_WAIT, evaluate_absolute)


def _rows(n_ok: int, n_bad: int):
    return ([{"outcome": "success", "variant": "live"}] * n_ok
            + [{"outcome": "tool_error", "variant": "live"}] * n_bad)


# --------------------------------------------------------------------------
# 核心:极低成功率 → wait(去查),不是 rollback(毁候选)
# --------------------------------------------------------------------------

def test_track_order_27_percent_does_not_roll_back():
    """**就是页面上那一行数字。** 14 成功 / 37 失败 = 0.27。

    修复前:ROLLBACK「成功率 0.27 低于下限 0.6」——把一份没问题的 skill 换掉,
    同时把真正的原因(评测订单号、压测流量、兜底被记成失败)全部掩盖。
    """
    r = evaluate_absolute(_rows(14, 37))
    assert r["decision"] == DECISION_WAIT, f"仍然会回滚: {r['reason']}"
    assert "先去查" in r["reason"] or "查" in r["reason"]
    assert "写坏" in r["reason"], "要明说这多半不是 skill 的问题"


def test_below_sanity_floor_waits():
    r = evaluate_absolute(_rows(3, 47))
    assert r["decision"] == DECISION_WAIT
    assert str(AB_SANITY_FLOOR) in r["reason"]


# --------------------------------------------------------------------------
# 反向:中间那一段仍然要回滚,不能把保护做成"永不回滚"
# --------------------------------------------------------------------------

def test_between_floor_and_min_rate_still_rolls_back():
    """0.3 ≤ rate < 0.6 是"确实不达标但数字还讲得通"的区间,自动化该收口。

    这条是本次改动的边界:保护不能宽到把真实的劣化也放过去,否则绝对值看门狗
    就等于没有。
    """
    r = evaluate_absolute(_rows(20, 30))   # 0.40
    assert r["decision"] == DECISION_ROLLBACK
    assert "0.40" in r["reason"]


def test_exactly_at_floor_still_rolls_back():
    """恰好等于下限不算"低于":边界要与 evaluate_ab 的 `<` 语义一致。"""
    r = evaluate_absolute(_rows(30, 70), sanity_floor=0.3)   # 0.30
    assert r["decision"] == DECISION_ROLLBACK


def test_healthy_rate_still_promotes():
    r = evaluate_absolute(_rows(45, 5))
    assert r["decision"] == DECISION_PROMOTE


def test_insufficient_samples_unchanged():
    """样本不足仍然先于一切判 wait——这次改动不该动到它。"""
    r = evaluate_absolute(_rows(0, 5))
    assert r["decision"] == DECISION_WAIT
    assert "样本不足" in r["reason"]


def test_sanity_floor_is_injectable():
    """与 evaluate_ab 同样的可注入形状(单测/离线复算要能改)。"""
    assert evaluate_absolute(_rows(14, 37), sanity_floor=0.1)["decision"] == DECISION_ROLLBACK
    assert evaluate_absolute(_rows(35, 15), sanity_floor=0.8)["decision"] == DECISION_WAIT


def test_rate_reported_unchanged():
    """判 wait 不代表把数字藏起来:canary_rate 仍要如实给出。

    "别据此回滚"和"别显示"是两件事——运维要看见 0.27 才知道去查什么。
    """
    r = evaluate_absolute(_rows(14, 37))
    assert r["canary_rate"] == pytest.approx(14 / 51)
    assert r["canary_samples"] == 51
