"""WS3 进化循环看板:只读聚合的口径测试(技术方案 §4)。

守三条:
1. live 口径只算 DECISION_SOURCES——压测轨迹不许混进成功率;
2. 看板数字与执行函数同源(gate_readiness / watchdog / hint_stats);
3. 只读:跑完 collect_status 不产生任何写入(轨迹/灰度/候选目录不变)。
"""

import json

from app.db.database import Database

_SKILL_MD = """---
name: track-order
description: 查询订单与物流进度。适用关键词:物流、发货、快递
---

先 `query_order` 核对订单,再答复买家。
"""


def _write_skill(root, name):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")


def _db_with_traces(tmp_path):
    d = Database(db_path=str(tmp_path / "loop.db"))
    d.init_schema()
    for outcome in ("success", "success", "tool_error"):
        d.record_skill_trace("s", "u1", "track-order", [], outcome, source="live")
    # 压测轨迹:全口径看得见,live 口径看不见(看门狗/看板同纪律)
    d.record_skill_trace("s", "ab0", "track-order", [], "tool_error", source="loadtest")
    return d


def test_collect_status_aggregates(tmp_path):
    from app.scripts.skill_loop_status import collect_status

    defs = tmp_path / "defs"
    cands = tmp_path / "cands"
    _write_skill(defs, "track-order")
    _write_skill(cands, "track-order")          # 改进型候选
    dataset = tmp_path / "cases.json"
    dataset.write_text("[]", encoding="utf-8")

    d = _db_with_traces(tmp_path)
    statuses = {s["skill"]: s for s in collect_status(
        db=d, definitions_dir=str(defs), candidates_dir=str(cands),
        dataset_path=str(dataset))}

    row = statuses["track-order"]
    assert row["live"]["counts"] == {"success": 2, "tool_error": 1}
    assert abs(row["live"]["rate"] - 2 / 3) < 1e-6
    assert row["all_sources"]["tool_error"] == 2          # 全口径含压测那条
    assert row["candidate"]["valid"] is True
    assert row["candidate"]["is_improvement"] is True
    assert row["gate"]["evaluable"] is False              # 空评测集=评不了,不是不达标
    assert row["gate"]["count"] == 0
    assert "gate_unavailable" in row["attention"]
    assert row["canary"] is None and row["decision"] is None
    assert json.dumps(statuses, ensure_ascii=False, default=str)  # --json 出口可用


def test_collect_status_includes_candidate_only_skills(tmp_path):
    """正式目录没有、只有候选的 skill 也要出现在看板里(否则新技能永远看不见)。"""
    from app.scripts.skill_loop_status import collect_status

    defs = tmp_path / "defs"
    cands = tmp_path / "cands"
    defs.mkdir()
    _write_skill(cands, "order-query")
    dataset = tmp_path / "cases.json"
    dataset.write_text("[]", encoding="utf-8")

    d = Database(db_path=str(tmp_path / "loop2.db"))
    d.init_schema()
    names = {s["skill"] for s in collect_status(
        db=d, definitions_dir=str(defs), candidates_dir=str(cands),
        dataset_path=str(dataset))}
    assert names == {"order-query"}


def test_collect_status_is_read_only(tmp_path):
    """看板跑完不许留下任何写入:轨迹条数、灰度表前后一致。"""
    from app.scripts.skill_loop_status import collect_status

    defs = tmp_path / "defs"
    cands = tmp_path / "cands"
    _write_skill(defs, "track-order")
    dataset = tmp_path / "cases.json"
    dataset.write_text("[]", encoding="utf-8")

    d = _db_with_traces(tmp_path)
    before_traces = sum(sum(v.values()) for v in d.skill_trace_counts().values())
    before_canaries = len(d.list_active_canaries())
    collect_status(db=d, definitions_dir=str(defs),
                   candidates_dir=str(cands), dataset_path=str(dataset))
    after_traces = sum(sum(v.values()) for v in d.skill_trace_counts().values())
    assert after_traces == before_traces
    assert len(d.list_active_canaries()) == before_canaries


def test_render_includes_skill_and_attention(tmp_path):
    from app.scripts.skill_loop_status import _render, collect_status

    defs = tmp_path / "defs"
    cands = tmp_path / "cands"
    _write_skill(defs, "track-order")
    dataset = tmp_path / "cases.json"
    dataset.write_text("[]", encoding="utf-8")
    d = _db_with_traces(tmp_path)
    text = _render(collect_status(db=d, definitions_dir=str(defs),
                                  candidates_dir=str(cands),
                                  dataset_path=str(dataset)))
    assert "track-order" in text
    assert "关注" in text
