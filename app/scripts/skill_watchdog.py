"""在线自进化收口:按风险档开灰度,按实战成绩自动转正 / 自动回滚。

用法:
  python -m app.scripts.skill_watchdog --start <skill-name>   按风险档开灰度/走门禁
  python -m app.scripts.skill_watchdog --start-all            对所有候选逐个执行 --start
  python -m app.scripts.skill_watchdog --check                评估活跃灰度并自动收口

分级授权(见 app/agent/skills/risk.py):
  low    改进型 + 只读工具 → 开 50% 灰度,跑赢自动转正、跑输自动回滚(全程无人);
  medium 新建 skill        → 过离线评测门禁即转正,之后按绝对成功率看门狗守着;
  high   碰钱/承诺类       → 只打印提示,**代码绝不自动转正**,必须人工执行
                             python -m app.scripts.promote_skill <name>

--check 自动转正用 force=True:灰度实战数据比离线评测是更强的证据。注意
promote() 内部的工具名校验照跑——force 从不放行编造工具名的候选。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.skills import risk as risk_mod  # noqa: E402
from app.agent.skills.tree_text import classify_tree_risk, validate_skill_tree  # noqa: E402
from app.agent.skills.watchdog import (  # noqa: E402
    DECISION_PROMOTE,
    DECISION_ROLLBACK,
    evaluate_ab,
    evaluate_absolute,
)
from app.scripts.promote_skill import (  # noqa: E402
    ARCHIVE_DIR,
    CANDIDATES_DIR,
    DEFINITIONS_DIR,
    list_candidates,
    promote,
    rollback,
)
from app.utils.console import enable_utf8_stdout  # noqa: E402


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _db():
    from app.db import get_db
    return get_db()


def _promote_outcome_action(promoted: dict, success_action: str) -> str:
    """把 `promote()` 的返回结果折算成看门狗要打的 action 标签。

    `block_on_high=True` 拦下来的失败必须打独立的 `promote_blocked_risk_changed`,
    不能和门禁/校验之类的普通失败混成一条谁都看不出该干什么的 `promote_failed`——
    `start_for_candidate` 的 GATE_THEN_WATCH 分支与 `check_canaries` 的 A/B 转正
    分支都是无人值守路径,经过 promote() 之后都要落到这一处,标签口径必须一致。
    """
    if promoted["promoted"]:
        return success_action
    risk_now = promoted.get("risk")
    if (risk_now is not None
            and risk_mod.promotion_policy(risk_now) == risk_mod.POLICY_MANUAL):
        return "promote_blocked_risk_changed"
    return "promote_failed"


# --start/--start-all 与 --check 共用的"需要人工介入"判定:任一结果的 action
# 落在这里面,说明本轮没能全自动收尾,main() 必须以非零码退出让 cron 能告警——
# 不能只有 --check 会退出非零,--start-all 同样可能因为 block_on_high 被拦而
# 需要人工,退出码却一直是 0 会让这类情况被 cron 当成普通成功。
#
# `gate_unavailable` 在列而 `gate_failed` 不在,这个不对称是刻意的:门禁**评了**
# 并判候选不达标,属于自动化正常收尾(候选该弃用,没人需要做什么);门禁**评不了**
# 则是卡死状态——不补用例、不人工放行,它下一轮、下一百轮都是同一行,而退出码
# 一直是 0 会让 cron 把"这个 skill 永远无法转正"当成普通成功。
NEEDS_HUMAN_ACTIONS = ("rollback_failed_manual_required", "promote_blocked_risk_changed",
                      "promote_blocked", "promote_failed", "gate_unavailable")


def _exit_if_needs_human(actions: list[str]) -> None:
    """`--start`/`--start-all`/`--check` 共用的收尾检查,见 `NEEDS_HUMAN_ACTIONS`。"""
    count = sum(1 for a in actions if a in NEEDS_HUMAN_ACTIONS)
    if count:
        print(f"\n⚠ {count} 项需人工处理(详情见上方各行),以非零码退出以便告警")
        sys.exit(1)


def start_for_candidate(skill_name: str, definitions_dir: str, candidates_dir: str,
                        archive_dir: str, db) -> dict:
    """按风险档决定该候选怎么放行。返回 {"risk","policy","action","detail"}。

    - high   → action="manual_required",不动任何东西;
    - low    → action="canary_started",登记 50% 灰度;
    - medium → action="gated"(过门禁则顺带转正) / "gate_failed"(门禁未过) /
               "promote_blocked_risk_changed"(门禁用的判档与 promote() 真正装机前
               对快照重新判档不一致,快照被判为高危,自动化转正拒绝放行,需人工)。
    """
    candidate = Path(candidates_dir) / skill_name / "SKILL.md"
    if not candidate.exists():
        return {"risk": None, "policy": None, "action": "missing",
                "detail": f"候选不存在: {candidate}"}

    # 校验与判档都按**整棵技能树**:候选带的 references/*.md 会随转正一起上线,
    # 并由 read_skill_file 灌进模型上下文 —— 只看根 SKILL.md 会让"人畜无害的正文
    # + 附件里写着直接全额退款"这种候选被判低危、走完全自动的灰度转正。
    report = validate_skill_tree(candidate.parent)
    if not report["valid"]:
        return {"risk": None, "policy": None, "action": "invalid",
                "detail": "; ".join(report["errors"])}

    is_new = not (Path(definitions_dir) / skill_name / "SKILL.md").exists()
    risk = classify_tree_risk(candidate.parent, is_new_skill=is_new, tree=report["tree"])
    policy = risk_mod.promotion_policy(risk)

    if policy == risk_mod.POLICY_MANUAL:
        return {"risk": risk, "policy": policy, "action": "manual_required",
                "detail": "高风险(碰钱/承诺类),需人工: "
                          f"python -m app.scripts.promote_skill {skill_name}"}

    if policy == risk_mod.POLICY_CANARY_AB:
        # 残留竞态(已知、按要求不加锁,记在这里而不是装作不存在):判档在这一行
        # 之上已经做完,`db.start_canary` 在这一行才登记。如果候选目录在这两行
        # 之间被并发的 synthesize_skills/improve_skill 原地改写,登记的灰度百分比
        # 会来自"判档时那份"而不是"实际开始服务的那份"。窗口远比本次修复关掉的
        # 转正 TOCTOU(check_canaries→promote 之间那段)小得多——只有判档和一次
        # DB 写入之间的间隙,没有磁盘 IO/网络 IO——接受它而不是加锁,理由与
        # 上传端点那处同级残留竞态一致(见 app/api/app.py `_process_skill_upload`
        # 里 `_skill_canary_block` 调用前的注释)。
        db.start_canary(skill_name, str(candidate), risk_mod.CANARY_PERCENT, risk, policy)
        return {"risk": risk, "policy": policy, "action": "canary_started",
                "detail": f"已开 {risk_mod.CANARY_PERCENT}% 灰度,等 --check 收口"}

    # POLICY_GATE_THEN_WATCH:新建 skill 无对照组,过离线门禁即转正,之后绝对值看门狗
    from app.agent.skills.gate import default_eval_fn, gate_candidate, gate_readiness
    from app.config.settings import settings

    ready = gate_readiness(skill_name, settings.eval_dataset_path)
    # **死结就在这里断开。** 一条用例都没有时先试着从真实会话合成一批,再决定
    # 要不要花钱跑评测。不合成的话,新蒸馏的 skill 会在这一行外面打
    # `gate_unavailable` —— 下一轮、下一百轮都是同一行。
    #
    # 合成本身不调 LLM、不改任何线上文件,断言全部取自真实工具调用与人工坐席
    # 回复(见 app/agent/skills/case_synthesis.py),所以放在无人值守路径里是
    # 安全的。它**不放松放行标准**:合成不出用例时照旧 fail-closed。
    if not ready["case_ids"] and settings.skill_gate_synth_enabled:
        try:
            from app.agent.skills.case_synthesis import synthesize_and_save

            stats = synthesize_and_save(skill_name, db=db)
            if stats.get("kept"):
                print(f"  ↳ 自动合成 {stats['kept']} 条门禁用例"
                      f"(轨迹 {stats.get('from_trace', 0)} / 关键词 "
                      f"{stats.get('from_keyword', 0)});未经人工审核")
                ready = gate_readiness(skill_name, settings.eval_dataset_path)
        except Exception as exc:  # noqa: BLE001 合成失败=回到原来的"没有用例",不改变判定
            print(f"  ↳ 门禁用例自动合成失败(按无用例处理): {exc}")

    case_ids = ready["case_ids"]
    gate_result = gate_candidate(
        skill_name=skill_name, candidate_path=str(candidate),
        definitions_dir=definitions_dir,
        dest_root=str(Path(archive_dir).parent / "_shadow"),
        eval_fn=default_eval_fn, case_ids=case_ids,
        tolerance=settings.skill_gate_tolerance,
    )
    if not gate_result["promote"]:
        # **"评不了" ≠ "评了没过"。** 这两种都会走到这里、都不放行,但要人做的事
        # 完全相反:后者是候选不达标,该改候选或弃用;前者是门禁自己不具备评估
        # 条件(没有用例 / 评测跑崩 / 没产出可比指标),候选可能一点问题都没有。
        #
        # 实测:蒸馏出来的**新建** skill 必然落在 `no_gate_cases` 这一支——新建
        # skill 无对照组所以走 gate_then_watch,而 gate_then_watch 要求过离线门禁,
        # 离线门禁要求评测集里有用例点名它(related_skills),而没有任何机制会为
        # 新 skill 产用例。于是 `--start-all` 每一轮都打同一行,永远如此。混成
        # `gate_failed` 时这一行读起来是"候选质量不行",没人会去想到要写用例。
        unavailable = gate_result.get("evaluable") is False
        detail = gate_result["reason"]
        if unavailable:
            if detail == "no_gate_cases":
                detail = "评测集里没有用例点名覆盖本 skill"
            detail = (f"门禁无法评估({detail}):候选未被否证,需先补该 skill 的门禁用例"
                      f"(评测集 related_skills 点名 {skill_name}),"
                      f"或人工放行 python -m app.scripts.promote_skill {skill_name} --force")
        return {"risk": risk, "policy": policy,
                "action": "gate_unavailable" if unavailable else "gate_failed",
                "detail": detail}

    # 门禁过了,但**过的是什么样的门禁**要跟着结论一起说。全自动路径上这条
    # detail 是唯一的记录:5 条全自动合成的"未劣化"与 5 条人工用例的"未劣化",
    # 在这一行里必须能分辨,否则日志翻回来时没人知道这次转正凭的是什么证据。
    if ready.get("synthetic_count"):
        gate_result["reason"] += (
            f"(门禁 {ready['count']} 条用例中 {ready['synthetic_count']} 条为自动合成、"
            f"未经人工审核,人工 {ready['human_count']} 条)")

    # block_on_high=True:门禁用的是上面 gate_candidate 那次判档时读到的候选,
    # 而 promote() 真正装机前会对**自己重新快照的那份字节**再判一次档——
    # 这里同样是无人值守的自动化路径,不该比 check_canaries 的转正宽松。
    promoted = promote(skill_name, definitions_dir, candidates_dir, archive_dir,
                       gate_result=gate_result, force=False, timestamp=_now_stamp(),
                       block_on_high=True)
    if promoted["promoted"]:
        # percent=0:已转正进正式目录,不需替换正文,只登记以便 --check 做绝对值监控
        db.start_canary(skill_name, str(candidate), 0, risk, policy)
    # 折算 action:被 block_on_high 拦下来的必须打独立的 promote_blocked_risk_changed,
    # 不能和门禁/校验之类的普通失败混成一条 promote_failed——否则无人值守的
    # --start-all 打出的这一行,和一次普通的"候选不达标"毫无区别,没人能看出
    # 这里其实是"高危,必须人工"。
    return {"risk": promoted.get("risk") or risk, "policy": policy,
            "action": _promote_outcome_action(promoted, "gated"),
            "detail": promoted["reason"]}


def _archive_rejected(skill_name: str, candidates_dir: str, archive_dir: str) -> str | None:
    """把落败候选从 _candidates/ 移到 _archive/<name>/rejected-<ts>/SKILL.md。

    保留以便复盘,同时确保它不再被 list_candidates 选中重开灰度。移动失败返回 None
    (不抛:收口流程不该因归档失败而中断)。
    """
    src_dir = Path(candidates_dir) / skill_name
    if not (src_dir / "SKILL.md").exists():
        return None
    try:
        dest_dir = Path(archive_dir) / skill_name / f"rejected-{_now_stamp()}"
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        # 整目录搬走:候选可能带参考资料,只搬 SKILL.md 会在候选区留下孤儿附件
        shutil.move(str(src_dir), str(dest_dir))
        return str(dest_dir / "SKILL.md")
    except OSError:
        return None


def check_canaries(definitions_dir: str, candidates_dir: str, archive_dir: str,
                   db) -> list[dict]:
    """评估所有活跃灰度并自动收口:转正 / 回滚 / 继续观察。

    收口铁律:**只有动作真的成功了才关闭灰度记录**。动作失败(如新建 skill
    绩效不达标却无历史版本可回滚)必须保持灰度为活跃并明确升级给人工——否则
    差劲的 skill 会永久留在线上,而库里却记着"已回滚",监控就此静默停止。
    """
    results: list[dict] = []
    for row in db.list_active_canaries():
        skill_name = row["skill_name"]
        is_ab = row.get("policy") == risk_mod.POLICY_CANARY_AB

        # 只取本轮灰度开始之后的轨迹:同一 skill 之前被废弃/已收口的灰度会留下
        # variant=canary 的旧行,混进来会污染本候选的成功率与样本数。
        started_at = str(row.get("started_at") or "")
        traces = [
            t for t in db.list_skill_traces(skill_name=skill_name, limit=1000)
            if str(t.get("created_at") or "") >= started_at
        ]

        if is_ab:
            verdict = evaluate_ab(traces, min_samples=risk_mod.CANARY_MIN_SAMPLES,
                                  max_drop=risk_mod.CANARY_MAX_DROP)
        else:
            verdict = evaluate_absolute(traces, min_samples=risk_mod.ABSOLUTE_MIN_SAMPLES,
                                        min_rate=risk_mod.ABSOLUTE_MIN_RATE)

        entry = {"skill_name": skill_name, "decision": verdict["decision"],
                 "reason": verdict["reason"], "live_rate": verdict["live_rate"],
                 "canary_rate": verdict["canary_rate"],
                 "canary_samples": verdict["canary_samples"], "action": "none"}

        if verdict["decision"] == DECISION_PROMOTE:
            if is_ab:
                # 灰度实战胜出 → 转正。**不再自己先读一遍候选目录重新判档**:
                # 那次预判(读 cand_dir → classify_tree_risk(cand_dir))和下面
                # promote() 内部真正快照之间还是隔着一段可写窗口——预判本身就是
                # 一次新的 TOCTOU 读,只是把窗口挪了个位置,并没有关掉它。
                # `promote(..., block_on_high=True)` 才是真正关掉窗口的地方:它
                # 对**自己快照下来的那份字节**判档,风险结果只从这里的返回值读
                # (`promoted["risk"]`),不再有"谁读的字节"和"谁装的字节"不一致
                # 的可能。force=True:灰度实战数据比离线评测是更强的证据,但校验
                # 与(block_on_high 的)风险门都照跑,不因为 force 被放行。
                promoted = promote(skill_name, definitions_dir, candidates_dir, archive_dir,
                                   gate_result=None, force=True, block_on_high=True,
                                   timestamp=_now_stamp())
                risk_now = promoted.get("risk")
                entry["risk"] = risk_now
                entry["detail"] = promoted["reason"]
                # 折算 action(与 start_for_candidate 的 GATE_THEN_WATCH 分支共用同一份
                # 口径,见 _promote_outcome_action):block_on_high 拦下来的必须显式
                # 打成 promote_blocked_risk_changed——候选在灰度期间(或本轮判档与
                # 安装之间那一瞬)变成了高危档,不能和"门禁/校验失败"之类的普通失败
                # 混成一条谁都看不出该干什么的日志。
                entry["action"] = _promote_outcome_action(promoted, "promoted")
                if promoted["promoted"]:
                    db.finish_canary(skill_name, "promoted")
                elif entry["action"] == "promote_blocked_risk_changed":
                    db.finish_canary(skill_name, "rolled_back")
                # 转正失败(非风险拦截)→ 灰度保持活跃,下轮再判(不留"已转正"的假记录)
            else:
                entry["action"] = "watch_passed"   # 绝对值达标,结束监控
                db.finish_canary(skill_name, "promoted")

        elif verdict["decision"] == DECISION_ROLLBACK:
            if is_ab:
                # 候选还没进正式目录,废弃灰度即等于回滚,不必动 definitions。
                # 但必须把落败候选移出 _candidates/,否则下次 --start-all 会对同一个
                # 已知烂候选再开一次 50% 灰度,反复拿真实流量试错。
                entry["action"] = "canary_discarded"
                moved = _archive_rejected(skill_name, candidates_dir, archive_dir)
                entry["detail"] = (f"落败候选已移至 {moved}" if moved
                                   else "落败候选移动失败,请手动清理 _candidates/")
                db.finish_canary(skill_name, "rolled_back")
            else:
                back = rollback(skill_name, definitions_dir, archive_dir)
                if back["rolled_back"]:
                    entry["action"] = "rolled_back"
                    entry["detail"] = back["reason"]
                    db.finish_canary(skill_name, "rolled_back")
                else:
                    # 新建 skill 无历史版本可恢复 → 无法自动撤回。灰度保持活跃,
                    # 明确升级人工:它此刻**仍在线上服务**,必须有人处理。
                    entry["action"] = "rollback_failed_manual_required"
                    entry["detail"] = (
                        f"绩效不达标但无法自动回滚({back['reason']})。"
                        f"该 skill 仍在线上生效,需人工处理:"
                        f"检查 definitions/{skill_name}/SKILL.md 并决定改进或移除")

        results.append(entry)
    return results


def main() -> None:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="Skill 灰度看门狗(在线自进化收口)")
    parser.add_argument("--start", metavar="SKILL", help="按风险档为该候选开灰度/走门禁")
    parser.add_argument("--start-all", action="store_true", help="对所有候选逐个执行 --start")
    parser.add_argument("--check", action="store_true", help="评估活跃灰度并自动收口")
    args = parser.parse_args()

    db = _db()

    if args.start or args.start_all:
        names = ([args.start] if args.start
                 else [c["name"] for c in list_candidates(CANDIDATES_DIR, DEFINITIONS_DIR)])
        if not names:
            print("没有候选(先跑 python -m app.scripts.synthesize_skills)")
        start_actions: list[str] = []
        for name in names:
            out = start_for_candidate(name, DEFINITIONS_DIR, CANDIDATES_DIR, ARCHIVE_DIR, db)
            print(f"[{out['risk'] or '-'}/{out['action']}] {name}: {out['detail']}")
            start_actions.append(out["action"])
        # --start/--start-all 是和 --check 一样的无人值守路径:一次自动转正被
        # block_on_high 拦下,不能因为这一分支从没退出过非零码,就让 cron 把它当成
        # 普通成功——升级逻辑必须和 --check 一致,见 NEEDS_HUMAN_ACTIONS。
        _exit_if_needs_human(start_actions)

    if args.check:
        results = check_canaries(DEFINITIONS_DIR, CANDIDATES_DIR, ARCHIVE_DIR, db)
        if not results:
            print("没有活跃灰度")
        for r in results:
            rates = (f"live={r['live_rate']} canary={r['canary_rate']} "
                     f"n={r['canary_samples']}")
            print(f"[{r['decision']}/{r['action']}] {r['skill_name']}: {r['reason']} | {rates}")
            # detail 里放的是可执行指引(尤其"无法自动回滚→该 skill 仍在线上,需人工处理"),
            # 不打出来等于护栏只改对了库里的记账,却没人知道要去处理。
            if r.get("detail"):
                print(f"    → {r['detail']}")

        _exit_if_needs_human([r["action"] for r in results])

    if not (args.start or args.start_all or args.check):
        parser.error("需要 --start / --start-all / --check 之一")


if __name__ == "__main__":
    main()
