"""门禁"评不了" ≠ 候选"评了没过"。

**实测缺陷**(走查 Skill 自进化闭环时抓到)。上传一份发票开具 SOP,蒸馏成功:

    created=True name=invoice-issuance risk=medium policy=gate_then_watch errors=[]

产物质量很好——引用的工具真实存在(validator: `valid: true, unknown_tools: []`)、
SOP 的禁止项也被保留了下来。然后转正:

    POST /api/admin/skills/invoice-issuance/promote               → 400 未跑评测门禁
    POST /api/admin/skills/invoice-issuance/promote?run_gate=true → 400 该技能没有门禁用例

**这个候选不可能通过界面上线,而这件事在页面加载时就是已知的**(数一下评测集即可)。

顺着往下查,这不是界面问题,是闭环的断点:

- 新建 skill 无对照组 → `risk=medium` 判到 `gate_then_watch`
- `gate_then_watch` 要求先过**离线门禁**才进灰度(skill_watchdog.py 的注释写得很清楚:
  "新建 skill 无对照组,过离线门禁即转正")
- 离线门禁要求评测集里有用例在 `related_skills` 里点名它
- **没有任何机制会为新 skill 产用例**:蒸馏不产、上传不带、界面上没有入口

于是每一个自动蒸馏出来的新 skill,`--start-all` 每一轮都打同一行、永远如此。而那一行
折算成的 action 是 `gate_failed`——读起来是"候选质量不行"。**没人会因为这行日志想到
要去写门禁用例。** 实测评测集 10 条用例里只有 3 条带 `related_skills`,4 个 live skill
里有 1 个(draft-outreach-campaign)自己就是 0 条:它连"改进后转正"也走不通。

本文件锁住两件事:

1. **evaluable 这个区分**:没有用例 / 评测崩了 / 没产出可比指标,三种都是
   `promote=False` 但都**不代表候选不达标**,必须与"评了判劣化"分开报。
2. **样本量披露**:实测有用例的 skill 都只有 1 条。门禁比的是 pass_rate 等三个**均值**
   (regression.py `_METRICS`),两侧各真调一次 LLM。n=1 时 tolerance=0.05 的含义退化成
   "只要没把唯一那条用例从过弄成不过就算未劣化",而一次随机波动也能凭空判出劣化。
   放行判定不动(动它会把仅存的那条通路也掐死),但 `promote=True` 后面必须紧跟着
   证据强度,不能让 n=1 的对比在日志和界面上长得跟一次真正的回归评测一模一样。

**没修的部分**(记在这里而不是装作已解决):蒸馏仍然不产门禁用例,新建 skill 仍然
只能靠人工 force 放行。让 LLM 产用例再由 LLM 评是不是自证、未经人审的用例能不能作为
门禁依据,是个需要拍板的产品问题,不该在一次走查里单方面定。已另开任务。
"""

import json

import pytest

from app.agent.skills.gate import MIN_TRUSTWORTHY_CASES, gate_candidate, gate_readiness


# --------------------------------------------------------------------------
# gate_readiness:在花钱之前就能回答"评不评得了、结论可不可信"
# --------------------------------------------------------------------------

def _dataset(tmp_path, cases):
    p = tmp_path / "cases.json"
    p.write_text(json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _case(cid, skills):
    return {"id": cid, "description": cid, "turns": ["你好"], "related_skills": skills}


def test_no_cases_is_not_evaluable(tmp_path):
    """核心断言:0 条用例 = 评不了,而且 note 要说出"这不是候选质量问题"。"""
    ds = _dataset(tmp_path, [_case("c1", ["other-skill"])])
    r = gate_readiness("invoice-issuance", ds)
    assert r["count"] == 0
    assert r["evaluable"] is False
    assert r["underpowered"] is False, "没有用例谈不上样本量不足,那是另一件事"
    assert "无法评估" in r["note"]
    assert "候选质量" in r["note"], "不说明这一点,操作者会去改一个没问题的候选"


def test_one_case_is_evaluable_but_underpowered(tmp_path):
    """1 条用例:跑得动,但结论是噪声级的——两件事都要说。"""
    ds = _dataset(tmp_path, [_case("c1", ["track-order"])])
    r = gate_readiness("track-order", ds)
    assert r["count"] == 1
    assert r["evaluable"] is True, "有用例就该跑,不能因为少就拒绝(那会掐死唯一的通路)"
    assert r["underpowered"] is True
    assert "噪声" in r["note"]


def test_enough_cases_is_clean(tmp_path):
    ds = _dataset(tmp_path, [_case(f"c{i}", ["track-order"])
                             for i in range(MIN_TRUSTWORTHY_CASES)])
    r = gate_readiness("track-order", ds)
    assert r["evaluable"] is True
    assert r["underpowered"] is False


def test_unreadable_dataset_is_unevaluable_not_empty(tmp_path):
    """数据集读不出来时必须是"评不了",不能退化成"没有相关用例"。

    后者听起来是个正常结论(这个 skill 就是没人写用例),前者才是真相(评测集坏了)。
    把故障说成正常结论,是这轮走查反复抓到的同一个错误。
    """
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    r = gate_readiness("track-order", str(p))
    assert r["evaluable"] is False
    assert "读取失败" in r["note"], f"故障被说成了正常结论: {r['note']}"


def test_missing_dataset_file_is_unevaluable(tmp_path):
    r = gate_readiness("track-order", str(tmp_path / "nope.json"))
    assert r["evaluable"] is False
    assert "无法评估" in r["note"]


# --------------------------------------------------------------------------
# gate_candidate:三种 promote=False,只有一种是"候选不达标"
# --------------------------------------------------------------------------

def _gate(case_ids, eval_fn, tmp_path, tolerance=0.05):
    defs = tmp_path / "definitions"
    (defs / "track-order").mkdir(parents=True)
    (defs / "track-order" / "SKILL.md").write_text("---\nname: track-order\n---\nx",
                                                   encoding="utf-8")
    cand = tmp_path / "_candidates" / "track-order"
    cand.mkdir(parents=True)
    (cand / "SKILL.md").write_text("---\nname: track-order\n---\ny", encoding="utf-8")
    return gate_candidate(skill_name="track-order", candidate_path=str(cand / "SKILL.md"),
                          definitions_dir=str(defs), dest_root=str(tmp_path / "_shadow"),
                          eval_fn=eval_fn, case_ids=case_ids, tolerance=tolerance)


def test_no_cases_marks_unevaluable(tmp_path):
    """核心断言:no_gate_cases 必须带 evaluable=False。

    下游(看门狗 action、界面文案)全靠这个字段区分"评不了"和"评了没过"。
    """
    g = _gate([], lambda d, c: {}, tmp_path)
    assert g["promote"] is False
    assert g["reason"] == "no_gate_cases"
    assert g["evaluable"] is False
    assert g["case_count"] == 0


def test_eval_crash_marks_unevaluable(tmp_path):
    """评测跑崩 = 评不了:候选没被否证,不该让人去改候选。"""
    def boom(d, c):
        raise RuntimeError("LLM 超时")

    g = _gate(["c1", "c2", "c3"], boom, tmp_path)
    assert g["promote"] is False, "fail-closed 不能被这次改动放松"
    assert g["evaluable"] is False
    assert "LLM 超时" in g["reason"]


def test_no_comparable_metrics_marks_unevaluable(tmp_path):
    """评测返回空 summary:既有的 fail-closed 保留,但归到"评不了"。"""
    g = _gate(["c1", "c2", "c3"], lambda d, c: {"summary": {}}, tmp_path)
    assert g["promote"] is False
    assert g["evaluable"] is False


def test_real_regression_is_evaluable_and_rejected(tmp_path):
    """**反向断言**:真的评出劣化时 evaluable=True——这才是"候选不达标"。

    这一条和上面三条一起才构成那个区分:不能把所有 promote=False 都标成评不了。
    """
    seen = []

    def ev(skills_dir, case_ids):
        seen.append(skills_dir)
        # 第一次是 baseline(现行),第二次是候选;让候选掉 0.4
        return {"summary": {"pass_rate": 0.9 if len(seen) == 1 else 0.5,
                            "avg_process_score": 0.9, "avg_result_score": 0.9}}

    g = _gate(["c1", "c2", "c3"], ev, tmp_path)
    assert g["promote"] is False
    assert g["evaluable"] is True, "评出来的劣化被标成了'评不了'"
    assert "劣化" in g["reason"]


def test_pass_on_one_case_carries_the_caveat(tmp_path):
    """n=1 通过时,reason 必须自带证据强度,不能只说"允许转正"。

    实测有门禁用例的三个 skill 全都只有 1 条。放行判定不变,但输出给人的那句话
    不能让它看起来跟一次真正的回归评测一样。
    """
    def ev(skills_dir, case_ids):
        return {"summary": {"pass_rate": 1.0, "avg_process_score": 0.9,
                            "avg_result_score": 0.9}}

    g = _gate(["only-one"], ev, tmp_path)
    assert g["promote"] is True, "别把仅存的这条通路掐死"
    assert g["underpowered"] is True
    assert g["case_count"] == 1
    assert "1 条用例" in g["reason"] and "证据强度" in g["reason"]


def test_pass_with_enough_cases_has_no_caveat(tmp_path):
    def ev(skills_dir, case_ids):
        return {"summary": {"pass_rate": 1.0, "avg_process_score": 0.9,
                            "avg_result_score": 0.9}}

    g = _gate([f"c{i}" for i in range(MIN_TRUSTWORTHY_CASES)], ev, tmp_path)
    assert g["promote"] is True
    assert g["underpowered"] is False
    assert "证据强度" not in g["reason"], "样本够了还加免责声明会让这句话失去意义"


# --------------------------------------------------------------------------
# 看门狗:两种失败必须打不同的 action,且"评不了"要让 cron 告警
# --------------------------------------------------------------------------

def test_watchdog_needs_human_on_gate_unavailable():
    """`gate_unavailable` 在 NEEDS_HUMAN_ACTIONS 里、`gate_failed` 不在。

    不对称是刻意的:门禁评了并否掉候选,是自动化正常收尾;门禁评不了则是卡死——
    不补用例、不人工放行,它下一百轮都是同一行,而退出码一直是 0 会让 cron 把
    "这个 skill 永远无法转正"当成普通成功。
    """
    from app.scripts.skill_watchdog import NEEDS_HUMAN_ACTIONS

    assert "gate_unavailable" in NEEDS_HUMAN_ACTIONS
    assert "gate_failed" not in NEEDS_HUMAN_ACTIONS


# --------------------------------------------------------------------------
# 真实评测集的现状(会随数据集演进而变,故只断言"字段在、能数出来")
# --------------------------------------------------------------------------

def test_live_skills_gate_coverage_is_reported():
    """对真实评测集调一遍,确保 gate_readiness 在生产配置上跑得通。

    实测(当时):track-order/process-return/product-recommend 各 1 条,
    draft-outreach-campaign 0 条。数字会变,所以这里只断言接口可用、三态自洽。
    """
    from app.config.settings import settings

    for name in ("track-order", "draft-outreach-campaign"):
        r = gate_readiness(name, settings.eval_dataset_path)
        assert isinstance(r["count"], int)
        assert r["evaluable"] is (r["count"] > 0)
        assert r["underpowered"] is (0 < r["count"] < MIN_TRUSTWORTHY_CASES)
        assert r["note"]
