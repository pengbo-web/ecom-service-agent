"""门禁同时读人工用例与合成用例——**但绝不把两者混成一个数字**。

自动合成解开了"新 skill 永远转不了正"的死结,代价是引入了一种新的骗法:一个
"5 条门禁用例"的 skill,如果那 5 条全是机器造的、没有人看过一眼,它与一个有 5 条
人工用例的 skill 在证据强度上完全不是一回事。只报总数会让操作者以为有人把过关。

本文件守三件事:
1. 两个来源都被读到(否则死结没解开);
2. 两个来源**分别报数**且合成的那部分被明确标注(否则解开死结的代价是骗人);
3. 合成用例**不进回归基线**(否则基线随自动合成漂移,失去参照作用)。
"""

import json

import pytest

from app.agent.skills.case_synthesis import save_synth_cases
from app.agent.skills.gate import gate_case_ids, gate_readiness, resolve_cases

HUMAN_DATASET = {
    "cases": [
        {"id": "case-track-1", "description": "人工用例", "turns": ["查订单"],
         "related_skills": ["track-order"], "expected_tools": ["query_order"]},
        {"id": "case-other-1", "description": "别的 skill", "turns": ["退货"],
         "related_skills": ["process-return"]},
    ]
}

SYNTH_CASES = [
    {"id": "synth-track-order-aaa", "description": "自动合成·未经人工审核 | track-order | 查物流",
     "turns": ["帮我查下 ORD-1 的物流"], "related_skills": ["track-order"],
     "expected_tools": ["query_order"]},
    {"id": "synth-track-order-bbb", "description": "自动合成·未经人工审核 | track-order | 到哪了",
     "turns": ["我的包裹到哪了"], "related_skills": ["track-order"],
     "expected_tools": ["query_logistics"]},
]


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(HUMAN_DATASET, ensure_ascii=False), encoding="utf-8")
    return str(path)


@pytest.fixture
def synth_root(tmp_path, monkeypatch):
    """把合成集目录指到 tmp,免得单测读到仓库里真实生成的那批。"""
    from app.config.settings import settings

    root = tmp_path / "cases_synth"
    monkeypatch.setattr(settings, "eval_synth_cases_dir", str(root))
    monkeypatch.setattr(settings, "skill_gate_synth_enabled", True)
    return root


# --------------------------------------------------------------------------
# 死结解开了没有
# --------------------------------------------------------------------------

def test_skill_with_zero_human_cases_becomes_evaluable(dataset, synth_root):
    """**这就是阶段一要解的那个死结。**

    改造前:`draft-outreach-campaign` 等 4 个现行 skill 的门禁用例数实测全是 0
    → `evaluable=False` → `--start-all` 每一轮都打 `gate_unavailable`,永远如此。
    """
    before = gate_readiness("draft-outreach-campaign", dataset)
    assert before["evaluable"] is False and before["count"] == 0

    save_synth_cases("draft-outreach-campaign", [
        {**c, "id": c["id"].replace("track-order", "draft-outreach-campaign"),
         "related_skills": ["draft-outreach-campaign"]} for c in SYNTH_CASES
    ], root=synth_root)

    after = gate_readiness("draft-outreach-campaign", dataset)
    assert after["evaluable"] is True and after["count"] == 2


def test_both_sources_are_read_human_first(dataset, synth_root):
    save_synth_cases("track-order", SYNTH_CASES, root=synth_root)
    ids = gate_case_ids("track-order", dataset)
    assert ids == ["case-track-1", "synth-track-order-aaa", "synth-track-order-bbb"]


def test_include_synthetic_false_returns_only_human(dataset, synth_root):
    save_synth_cases("track-order", SYNTH_CASES, root=synth_root)
    assert gate_case_ids("track-order", dataset, include_synthetic=False) == ["case-track-1"]


def test_kill_switch_returns_the_system_to_its_previous_behaviour(dataset, synth_root,
                                                                 monkeypatch):
    """`skill_gate_synth_enabled=False` 必须让门禁**完全**回到只认人工用例的状态。

    一个自动生成裁判尺的功能必须有一键退回:出问题时的第一动作是止血,不是调参。
    """
    from app.config.settings import settings

    save_synth_cases("track-order", SYNTH_CASES, root=synth_root)
    monkeypatch.setattr(settings, "skill_gate_synth_enabled", False)
    assert gate_case_ids("track-order", dataset) == ["case-track-1"]
    assert gate_readiness("track-order", dataset)["synthetic_count"] == 0


# --------------------------------------------------------------------------
# 分别报数 + 标注
# --------------------------------------------------------------------------

def test_readiness_reports_the_two_counts_separately(dataset, synth_root):
    save_synth_cases("track-order", SYNTH_CASES, root=synth_root)
    r = gate_readiness("track-order", dataset)
    assert (r["count"], r["human_count"], r["synthetic_count"]) == (3, 1, 2)


def test_note_says_out_loud_that_some_cases_were_not_reviewed(dataset, synth_root):
    """note 是界面与看门狗日志上唯一会被人读到的那句话。"""
    save_synth_cases("track-order", SYNTH_CASES, root=synth_root)
    note = gate_readiness("track-order", dataset)["note"]
    assert "未经人工审核" in note and "2 条" in note


def test_all_human_cases_produce_no_unaudited_warning(dataset, synth_root):
    """没有合成用例时不能凭空出现"未经人工审核"——那会让这句警告变成噪声,
    人读几次之后就不再读它了。"""
    assert "未经人工审核" not in gate_readiness("track-order", dataset)["note"]


def test_underpowered_note_still_carries_the_synthetic_disclosure(dataset, synth_root):
    """样本不足与"没人审过"是**两件独立的坏消息**,不能因为报了前者就吞掉后者。"""
    save_synth_cases("track-order", SYNTH_CASES[:1], root=synth_root)
    note = gate_readiness("track-order", dataset)["note"]
    assert "证据强度" in note or "运行噪声" in note
    assert "未经人工审核" in note


# --------------------------------------------------------------------------
# 用例真的取得到(否则门禁跑 0 条,看起来和从前一模一样)
# --------------------------------------------------------------------------

def test_resolve_cases_finds_synthetic_ids(synth_root, monkeypatch, tmp_path):
    """**最容易漏的一步。** `gate_case_ids` 交出合成 id,而 `default_eval_fn` 若
    只读 cases.json,一条都匹配不上 → 跑 0 条 → summary 空 → 门禁 fail-closed 打
    "评测未产出可比指标"。日志上看和从前的 gate_unavailable 毫无区别。
    """
    from app.config.settings import settings

    path = tmp_path / "cases.json"
    path.write_text(json.dumps(HUMAN_DATASET, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(settings, "eval_dataset_path", str(path))
    save_synth_cases("track-order", SYNTH_CASES, root=synth_root)

    got = resolve_cases(["case-track-1", "synth-track-order-aaa", "synth-track-order-bbb"])
    assert [c.id for c in got] == ["case-track-1", "synth-track-order-aaa",
                                   "synth-track-order-bbb"]
    assert got[1].expected_tools == ["query_order"]


def test_resolve_cases_skips_ids_that_no_longer_exist(synth_root, monkeypatch, tmp_path):
    from app.config.settings import settings

    path = tmp_path / "cases.json"
    path.write_text(json.dumps(HUMAN_DATASET, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(settings, "eval_dataset_path", str(path))
    assert [c.id for c in resolve_cases(["case-track-1", "gone"])] == ["case-track-1"]


# --------------------------------------------------------------------------
# 回归基线不能被污染
# --------------------------------------------------------------------------

def test_real_human_dataset_contains_no_synthetic_cases():
    """**仓库级不变量。** `cases.json` 同时是回归基线的采样集
    (`eval_baseline_path` 的来源)。混入未经人工审核的用例,基线会随着每一次自动
    合成而漂移,从此失去"参照"这个唯一作用——而且没有任何报错。
    """
    from app.agent.skills.case_synthesis import is_synthetic_case_id
    from app.config.settings import settings
    from app.evaluation.dataset import load_dataset

    leaked = [c.id for c in load_dataset(settings.eval_dataset_path)
              if is_synthetic_case_id(c.id)]
    assert not leaked, f"合成用例混进了人工回归集: {leaked}"


def test_synth_dir_is_not_the_dataset_path():
    """两者必须是不同的文件。配错成同一个路径,上一条测试要到下一次合成之后才会红。"""
    from pathlib import Path

    from app.config.settings import settings

    assert Path(settings.eval_synth_cases_dir).resolve() != \
        Path(settings.eval_dataset_path).resolve().parent / "cases.json"
    assert Path(settings.eval_dataset_path).name == "cases.json"
