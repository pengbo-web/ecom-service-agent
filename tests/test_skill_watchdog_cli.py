"""看门狗 CLI:高危候选只列出不自动上线;低危候选灰度跑赢自动转正、跑输自动回滚。"""

from pathlib import Path

from app.db import Database
from app.scripts.skill_watchdog import check_canaries, start_for_candidate

LIVE_MD = """---
name: process-return
description: 退货处理(现行版)。
---
第一步：调用 `query_order` 核对。
"""

READONLY_CANDIDATE = """---
name: process-return
description: 退货处理(候选版,只读)。
---
第一步：调用 `query_order` 与 `list_user_orders` 核对。
"""

REFUND_CANDIDATE = """---
name: process-return
description: 退货处理(候选版,含退款)。
---
第一步：调用 `apply_refund` 提交退款。
"""


def _setup(tmp_path, candidate_md):
    definitions = tmp_path / "definitions"
    (definitions / "process-return").mkdir(parents=True)
    (definitions / "process-return" / "SKILL.md").write_text(LIVE_MD, encoding="utf-8")

    candidates = definitions / "_candidates"
    (candidates / "process-return").mkdir(parents=True)
    (candidates / "process-return" / "SKILL.md").write_text(candidate_md, encoding="utf-8")

    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    return str(definitions), str(candidates), str(definitions / "_archive"), db


def test_start_puts_readonly_improvement_into_canary(tmp_path):
    definitions, candidates, archive, db = _setup(tmp_path, READONLY_CANDIDATE)

    result = start_for_candidate("process-return", definitions, candidates, archive, db)

    assert result["risk"] == "low"
    assert result["action"] == "canary_started"
    row = db.get_active_canary("process-return")
    assert row["percent"] == 50
    assert row["policy"] == "canary_ab"


def test_start_refuses_high_risk_candidate(tmp_path):
    definitions, candidates, archive, db = _setup(tmp_path, REFUND_CANDIDATE)

    result = start_for_candidate("process-return", definitions, candidates, archive, db)

    assert result["risk"] == "high"
    assert result["action"] == "manual_required"
    assert db.get_active_canary("process-return") is None   # 高危不进灰度
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == LIVE_MD       # 正式目录未被改动


def test_check_promotes_winning_canary(tmp_path):
    definitions, candidates, archive, db = _setup(tmp_path, READONLY_CANDIDATE)
    start_for_candidate("process-return", definitions, candidates, archive, db)

    for _ in range(10):
        db.record_skill_trace("s", "u", "process-return", [], "handoff", variant="live")
    for _ in range(12):
        db.record_skill_trace("s", "u", "process-return", [], "success", variant="canary")

    results = check_canaries(definitions, candidates, archive, db)

    assert results[0]["decision"] == "promote"
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == READONLY_CANDIDATE   # 已自动转正
    assert db.get_active_canary("process-return") is None           # 灰度已收口


def test_check_rolls_back_losing_canary(tmp_path):
    definitions, candidates, archive, db = _setup(tmp_path, READONLY_CANDIDATE)
    start_for_candidate("process-return", definitions, candidates, archive, db)

    for _ in range(10):
        db.record_skill_trace("s", "u", "process-return", [], "success", variant="live")
    for _ in range(12):
        db.record_skill_trace("s", "u", "process-return", [], "handoff", variant="canary")

    results = check_canaries(definitions, candidates, archive, db)

    assert results[0]["decision"] == "rollback"
    live = tmp_path / "definitions" / "process-return" / "SKILL.md"
    assert live.read_text(encoding="utf-8") == LIVE_MD    # 正式版没被换掉
    assert db.get_active_canary("process-return") is None  # 灰度已废弃


def test_check_waits_on_insufficient_samples(tmp_path):
    definitions, candidates, archive, db = _setup(tmp_path, READONLY_CANDIDATE)
    start_for_candidate("process-return", definitions, candidates, archive, db)
    db.record_skill_trace("s", "u", "process-return", [], "success", variant="canary")

    results = check_canaries(definitions, candidates, archive, db)

    assert results[0]["decision"] == "wait"
    assert db.get_active_canary("process-return") is not None   # 继续观察


# ---------- 收口铁律:动作失败不得留下"已收口"的假记录 ----------

def test_absolute_rollback_without_backup_keeps_canary_active(tmp_path):
    """新建 skill 绩效不达标但无历史版本可回滚:不得静默关闭灰度。

    否则差劲的 skill 永久留在线上,库里却记着"已回滚",监控静默停止。
    """
    definitions, candidates, archive, db = _setup(tmp_path, READONLY_CANDIDATE)
    cand_path = str(Path(candidates) / "process-return" / "SKILL.md")
    # 模拟"新建 skill 已转正、进入绝对值监控"的状态:percent=0 + 无任何备份
    db.start_canary("process-return", cand_path, 0, "medium", "gate_then_watch")

    for _ in range(30):      # 样本足够且成功率远低于下限
        db.record_skill_trace("s", "u", "process-return", [], "handoff", variant="live")

    results = check_canaries(definitions, candidates, archive, db)

    assert results[0]["decision"] == "rollback"
    assert results[0]["action"] == "rollback_failed_manual_required"
    assert "仍在线上生效" in results[0]["detail"]
    assert db.get_active_canary("process-return") is not None   # 灰度保持活跃,监控不停


def test_check_ignores_traces_from_before_this_canary(tmp_path):
    """上一轮灰度留下的旧 canary 轨迹不得混进本轮样本。"""
    definitions, candidates, archive, db = _setup(tmp_path, READONLY_CANDIDATE)

    # 旧轮次:20 条失败的 canary 轨迹(时间戳早于本轮灰度)
    conn = db.connect()
    try:
        for i in range(20):
            conn.execute(
                "INSERT INTO skill_traces (session_id, user_id, skill_name, tool_calls, "
                "outcome, created_at, variant) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"old{i}", "u", "process-return", "[]", "handoff",
                 "2000-01-01 00:00:00", "canary"))
        conn.commit()
    finally:
        conn.close()

    start_for_candidate("process-return", definitions, candidates, archive, db)

    results = check_canaries(definitions, candidates, archive, db)

    # 本轮尚无任何轨迹 → 应为 wait(若把 20 条旧失败算进来会直接判 rollback)
    assert results[0]["decision"] == "wait"
    assert results[0]["canary_samples"] == 0
