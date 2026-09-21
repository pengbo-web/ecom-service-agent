"""WS3 进化循环看板:per-skill 聚合自进化闭环各阶段状态(技术方案 §4)。

用法:
  python -m app.scripts.skill_loop_status                 # 全部 skill
  python -m app.scripts.skill_loop_status --skill track-order
  python -m app.scripts.skill_loop_status --json          # 机器可读

**纯只读聚合,不做任何判定、不写库。** 每个数字都来自执行侧同一个函数
(watchdog.evaluate_* / gate.gate_readiness / attribution.attribute /
risk 判档 / memory_hints.hint_stats),本脚本只渲染——防止"看板口径"与
"执行口径"分叉(本仓库反复吃过手抄表 drift 的亏)。

NEEDS_HUMAN 的**权威通道仍是 `skill_watchdog` 的非零退出码**(cron 告警);
本看板的 attention 段是它的可视化投影,不是第二个权威源。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.runtime_context import DECISION_SOURCES  # noqa: E402
from app.agent.skills import attribution, cohort_stats, memory_hints, watchdog  # noqa: E402
from app.agent.skills.gate import gate_readiness  # noqa: E402
from app.agent.skills.risk import POLICY_MANUAL, promotion_policy  # noqa: E402
from app.agent.skills.tree_text import classify_tree_risk, validate_skill_tree  # noqa: E402
from app.scripts.promote_skill import (  # noqa: E402
    CANDIDATES_DIR,
    DEFINITIONS_DIR,
    list_candidates,
)
from app.utils.console import enable_utf8_stdout  # noqa: E402

DEFAULT_DATASET = str(ROOT / "app" / "evaluation" / "cases.json")
_FAILED_OUTCOMES = ["tool_error", "handoff"]


def _rate(counts: dict[str, int]) -> float | None:
    """成功率 = success / 全部结局。0 条 → None:'没数据'不等于'全失败'。

    不预舍入:看板渲染时才格式化,机器出口(--json)给全精度,防"舍入后的
    数字再被下游当判据"这类口径漂移。
    """
    total = sum(counts.values())
    if not total:
        return None
    return counts.get("success", 0) / total


def _live_counts(db, skill_name: str) -> dict[str, int]:
    counts = db.skill_trace_counts(sources=list(DECISION_SOURCES))
    return dict(counts.get(skill_name, {}))


def _attribution_distribution(db, skill_name: str) -> dict[str, int]:
    """live 口径失败轨迹的归因分布。不给 client/model:看板只跑规则判据,
    不为看一眼状态花 LLM 调用;method 会如实标 rule。"""
    out: dict[str, int] = {}
    traces = db.list_skill_traces(skill_name=skill_name, outcomes=_FAILED_OUTCOMES,
                                  limit=500, sources=list(DECISION_SOURCES))
    for trace in traces:
        try:
            category = attribution.attribute(trace, db=db)["category"]
        except Exception:  # noqa: BLE001 单条归因炸了不让整张看板崩
            category = "error"
        out[category] = out.get(category, 0) + 1
    return out


def _candidate_risk(candidate: dict) -> str | None:
    try:
        report = validate_skill_tree(Path(candidate["path"]).parent)
        if not report["valid"]:
            return None
        return classify_tree_risk(Path(candidate["path"]).parent,
                                  is_new_skill=not candidate["is_improvement"],
                                  tree=report["tree"])
    except Exception:  # noqa: BLE001 判档炸了按"判不了"处理,绝不猜低危
        return None


def _decision(db, skill_name: str, canary: dict | None) -> dict | None:
    """活跃灰度的当前判定(只算不收口)。与 skill_watchdog --check 同口径:
    判定类调用必须显式传 live 来源。"""
    if canary is None:
        return None
    rows = db.list_skill_traces(skill_name=skill_name, limit=1000,
                                sources=list(DECISION_SOURCES))
    if int(canary.get("percent") or 0) > 0:
        return watchdog.evaluate_ab(rows)
    return watchdog.evaluate_absolute(rows)


def collect_status(db=None, definitions_dir: str = DEFINITIONS_DIR,
                   candidates_dir: str = CANDIDATES_DIR,
                   dataset_path: str = DEFAULT_DATASET) -> list[dict]:
    if db is None:
        from app.db import get_db
        db = get_db()

    candidates = {c["name"]: c for c in list_candidates(candidates_dir, definitions_dir)}
    names: list[str] = []
    def_root = Path(definitions_dir)
    if def_root.exists():
        names += [d.name for d in sorted(def_root.iterdir())
                  if d.is_dir() and not d.name.startswith("_")
                  and (d / "SKILL.md").exists()]
    names += [n for n in sorted(candidates) if n not in names]

    active_canaries = {c["skill_name"]: c for c in db.list_active_canaries()}
    hints_all = memory_hints.hint_stats(db=db)

    statuses: list[dict] = []
    for name in names:
        candidate = candidates.get(name)
        risk = _candidate_risk(candidate) if candidate else None
        canary = active_canaries.get(name)
        decision = _decision(db, name, canary)
        live = _live_counts(db, name)

        attention: list[str] = []
        if candidate is not None and not candidate["valid"]:
            attention.append("candidate_invalid")
        if risk is not None and promotion_policy(risk) == POLICY_MANUAL:
            attention.append("manual_required")
        if candidate is not None:
            ready = gate_readiness(name, dataset_path)
            gate = {k: ready[k] for k in
                    ("count", "human_count", "synthetic_count", "evaluable", "underpowered")}
            if not ready["evaluable"]:
                attention.append("gate_unavailable")
        else:
            gate = None
        if decision is not None and decision["decision"] == watchdog.DECISION_ROLLBACK:
            attention.append("rollback_pending")
        if canary is not None:
            attention.append("canary_active")

        statuses.append({
            "skill": name,
            "live": {"counts": live, "rate": _rate(live)},
            "all_sources": db.skill_trace_counts().get(name, {}),
            "hints": hints_all.get(name, {}),
            "attribution": _attribution_distribution(db, name),
            "candidate": ({k: candidate[k] for k in ("valid", "is_improvement", "errors")}
                          if candidate else None),
            "risk": risk,
            "gate": gate,
            "canary": ({k: canary[k] for k in ("percent", "policy", "started_at")}
                       if canary else None),
            "decision": decision,
            "attention": attention,
        })
    return statuses


def _render(statuses: list[dict]) -> str:
    lines: list[str] = []
    for s in statuses:
        live = s["live"]
        rate = "n/a" if live["rate"] is None else f"{live['rate']:.0%}"
        lines.append(f"{s['skill']}:")
        lines.append(
            f"  运行  live {sum(live['counts'].values())} 轮 · 成功率 {rate}"
            f" · 全口径 {sum(s['all_sources'].values())} 轮")
        hints = s["hints"]
        lines.append(f"  标记  {hints if hints else '无'}")
        lines.append(f"  归因  {s['attribution'] if s['attribution'] else '无失败轨迹'}")
        if s["candidate"]:
            lines.append(f"  候选  valid={s['candidate']['valid']}"
                         f" · 改进型={s['candidate']['is_improvement']}"
                         f" · 风险档={s['risk']}")
        else:
            lines.append("  候选  无")
        if s["gate"]:
            g = s["gate"]
            lines.append(f"  裁判尺 {g['count']} 条(人工 {g['human_count']}"
                         f" + 合成 {g['synthetic_count']}) · evaluable={g['evaluable']}"
                         f" · underpowered={g['underpowered']}")
        if s["canary"]:
            lines.append(f"  灰度  percent={s['canary']['percent']}"
                         f" · policy={s['canary']['policy']}")
            if s["decision"]:
                lines.append(f"  判定  {s['decision']['decision']}"
                             f" · {s['decision']['reason']}")
        lines.append(f"  关注  {', '.join(s['attention']) if s['attention'] else '无'}")
        lines.append("")
    return "\n".join(lines)


def _render_cohorts(rows: list[dict]) -> str:
    """WS4 群体段:**只报不回流**。率值 None 显示 n/a——样本不足不是 0 失败。"""
    if not rows:
        return "群体(只报不回流):无标签数据"
    lines = ["群体(只报不回流;率值 n/a = 样本不足,纪律同 anomaly_scan 的 min_samples):"]
    for r in rows:
        handoff = "n/a" if r["handoff_rate"] is None else f"{r['handoff_rate']:.0%}"
        tool_err = "n/a" if r["tool_error_rate"] is None else f"{r['tool_error_rate']:.0%}"
        lines.append(f"  {r['tag']:<16} 用户 {r['users']} · 轨迹 {r['traces']}"
                     f" · 转人工 {handoff} · 工具失败 {tool_err}")
    return "\n".join(lines)


def main() -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="Skill 自进化循环看板(只读)")
    parser.add_argument("--skill", default=None, help="只看一个 skill")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    statuses = collect_status()
    if args.skill:
        statuses = [s for s in statuses if s["skill"] == args.skill]
    cohorts = cohort_stats.cohort_report()
    if args.json:
        print(json.dumps({"skills": statuses, "cohorts": cohorts},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(_render(statuses))
        print(_render_cohorts(cohorts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
