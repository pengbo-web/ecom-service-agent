"""看门狗:按实战轨迹判定灰度候选该转正、该回滚还是继续观察。

两种模式对应 risk.promotion_policy 的两种放行方式:
- evaluate_ab:改进型候选有对照组(同 skill 的现行版) → 比成功率;
- evaluate_absolute:新建型候选没有对照组(线上原本没这个 skill) → 看绝对成功率。

都是纯函数(入参是 skill_traces 行,不碰 DB),便于单测与离线复算。
"""

from __future__ import annotations

from app.agent.skills.canary import VARIANT_CANARY, VARIANT_LIVE
from app.agent.skills.execution_trace import OUTCOME_SUCCESS

DECISION_PROMOTE = "promote"
DECISION_ROLLBACK = "rollback"
DECISION_WAIT = "wait"


def success_rate(rows: list[dict]) -> float | None:
    """成功轮占比;空样本返回 None(区别于 0.0——"没数据"不等于"全失败")。"""
    if not rows:
        return None
    ok = sum(1 for r in rows if r.get("outcome") == OUTCOME_SUCCESS)
    return ok / len(rows)


def _result(decision: str, reason: str, live_rows: list[dict], canary_rows: list[dict]) -> dict:
    return {
        "decision": decision, "reason": reason,
        "live_rate": success_rate(live_rows), "canary_rate": success_rate(canary_rows),
        "live_samples": len(live_rows), "canary_samples": len(canary_rows),
    }


def evaluate_ab(traces: list[dict], min_samples: int = 10, max_drop: float = 0.1,
                sanity_floor: float | None = None) -> dict:
    """A/B 判定:候选相对现行版掉点超过 max_drop 即回滚,否则转正。

    - 灰度样本不足 min_samples → wait(样本太少的胜负是噪声);
    - 没有 live 对照样本 → wait(该走 evaluate_absolute);
    - **两臂成功率都低于 `sanity_floor` → wait**(见下);
    - 老轨迹缺 variant 字段的按 live 计(兼容 Task 13 迁移前的数据)。

    **为什么需要 `sanity_floor`**:A/B 只问"有没有比现行版更差",从不问
    "够不够好"。现行版被基础设施故障压塌时,`canary_rate < live_rate - max_drop`
    会变成一个永远不成立的条件——live=0.03 时它等价于 `canary_rate < -0.07`,
    于是**任何候选都会自动转正**,包括绝对水平同样糟糕的那些。

    判 `wait` 而不是 `rollback`:两臂都低说明**问题多半不在候选身上**(依赖挂了、
    数据源不对),回滚会把锅扣给一份可能没问题的候选,还会掩盖真正的故障。
    正确的动作是停下来去查依赖。

    只在**两臂都低**时才拦:live 低而 canary 高,恰恰是一次真实的改进,必须放行。
    """
    if sanity_floor is None:
        from app.agent.skills.risk import AB_SANITY_FLOOR
        sanity_floor = AB_SANITY_FLOOR
    live = [t for t in traces if (t.get("variant") or VARIANT_LIVE) == VARIANT_LIVE]
    canary = [t for t in traces if t.get("variant") == VARIANT_CANARY]

    if len(canary) < min_samples:
        return _result(DECISION_WAIT,
                       f"灰度样本不足({len(canary)}/{min_samples})", live, canary)

    live_rate = success_rate(live)
    if live_rate is None:
        return _result(DECISION_WAIT, "无 live 对照样本,无法 A/B 判定", live, canary)
    if len(live) < min_samples:
        # 对照臂样本同样要够:1 条 live(恰好失败,rate=0)会让任何候选都"不劣化"而自动上线
        return _result(DECISION_WAIT,
                       f"对照样本不足({len(live)}/{min_samples})", live, canary)

    canary_rate = success_rate(canary)
    if canary_rate < sanity_floor and live_rate < sanity_floor:
        return _result(
            DECISION_WAIT,
            f"两臂成功率都低于可信下限(候选 {canary_rate:.2f} / 现行 {live_rate:.2f}"
            f",下限 {sanity_floor})——多半是依赖或数据源出了问题,"
            f"先查依赖,不在这种基线上做转正判定",
            live, canary)
    if canary_rate < live_rate - max_drop:
        return _result(
            DECISION_ROLLBACK,
            f"候选成功率 {canary_rate:.2f} 低于现行 {live_rate:.2f}(容差 {max_drop})",
            live, canary)
    return _result(
        DECISION_PROMOTE,
        f"候选成功率 {canary_rate:.2f} 未劣于现行 {live_rate:.2f}(容差 {max_drop})",
        live, canary)


def evaluate_absolute(traces: list[dict], min_samples: int = 30,
                      min_rate: float = 0.6) -> dict:
    """绝对值判定:没有对照组时看该 skill 自身成功率是否守住下限。"""
    rows = list(traces)
    if len(rows) < min_samples:
        return _result(DECISION_WAIT, f"样本不足({len(rows)}/{min_samples})", [], rows)

    rate = success_rate(rows)
    if rate < min_rate:
        return _result(DECISION_ROLLBACK,
                       f"成功率 {rate:.2f} 低于下限 {min_rate}", [], rows)
    return _result(DECISION_PROMOTE, f"成功率 {rate:.2f} 达标(下限 {min_rate})", [], rows)
