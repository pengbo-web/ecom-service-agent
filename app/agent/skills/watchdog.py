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
                      min_rate: float = 0.6,
                      sanity_floor: float | None = None) -> dict:
    """绝对值判定:没有对照组时看该 skill 自身成功率是否守住下限。

    **`sanity_floor` 补的是与 `evaluate_ab` 之间的一处不对称**(走查时抓到)。
    `evaluate_ab` 已经拒绝在"两臂都极低"的基线上做判定,理由它自己写着:
    "多半是依赖或数据源出了问题…回滚会把锅扣给一份可能没问题的候选,还会掩盖
    真正的故障"。而这条绝对值路径原来直接 `rate < min_rate → ROLLBACK`,
    同一个依赖故障在这里会把一份正常的 skill 回滚掉,理由写成"成功率 0.27 低于
    下限 0.6"——读起来是 skill 质量问题。

    实测那 0.27 是怎么来的(track-order,51 条轨迹):

    - 36 条来自合成用户(`ab*` 压测 / `ev*` 评测 / `trk*`),只有 15 条来自真实买家;
    - 36/37 条失败是同一个订单号 `ORD-20240115-001` ——**评测数据集里那个**;
    - 37 条 `tool_error` 里有 11 条其实兜底成功了(失败后的调用成功),而
      track-order 的描述里明写着"支持订单号不存在时的友好兜底"——**它正因为有
      兜底而被扣分**。

    也就是说这个 0.27 压根不是关于这份 skill 的陈述。低于 `sanity_floor` 时判
    `wait` 而不是 `rollback`,与 `evaluate_ab` 同一条取舍:**停下来去查,而不是
    先把候选毁掉**。回滚不可白做——它会把线上换成上一版并结束灰度。

    `min_rate` 与 `sanity_floor` 之间那一段(0.3 ≤ rate < 0.6)仍然回滚:那是
    "确实不达标但数字还讲得通"的区间,自动化该收口。
    """
    if sanity_floor is None:
        from app.agent.skills.risk import AB_SANITY_FLOOR
        sanity_floor = AB_SANITY_FLOOR
    rows = list(traces)
    if len(rows) < min_samples:
        return _result(DECISION_WAIT, f"样本不足({len(rows)}/{min_samples})", [], rows)

    rate = success_rate(rows)
    if rate < sanity_floor:
        return _result(
            DECISION_WAIT,
            f"成功率 {rate:.2f} 低于可信下限 {sanity_floor}——这么低多半是依赖、"
            f"数据源或轨迹口径出了问题,而不是这份 skill 写坏了;先去查,"
            f"不在这种基线上做回滚判定",
            [], rows)
    if rate < min_rate:
        return _result(DECISION_ROLLBACK,
                       f"成功率 {rate:.2f} 低于下限 {min_rate}", [], rows)
    return _result(DECISION_PROMOTE, f"成功率 {rate:.2f} 达标(下限 {min_rate})", [], rows)
