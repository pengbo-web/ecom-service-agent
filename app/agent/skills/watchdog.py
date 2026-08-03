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


def evaluate_ab(traces: list[dict], min_samples: int = 10, max_drop: float = 0.1) -> dict:
    """A/B 判定:候选相对现行版掉点超过 max_drop 即回滚,否则转正。

    - 灰度样本不足 min_samples → wait(样本太少的胜负是噪声);
    - 没有 live 对照样本 → wait(该走 evaluate_absolute);
    - 老轨迹缺 variant 字段的按 live 计(兼容 Task 13 迁移前的数据)。
    """
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
