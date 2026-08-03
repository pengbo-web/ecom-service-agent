"""G2 数据层：skill_traces 表读写（记录每轮 skill 执行轨迹，供失败采集/门禁分析）。"""

from app.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    return db


def test_record_and_list_skill_trace(tmp_path):
    db = _db(tmp_path)
    db.record_skill_trace(
        "s1", "u1", "process-return",
        [{"name": "query_order", "ok": True, "error": None}],
        "success",
    )

    rows = db.list_skill_traces()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "s1"
    assert row["user_id"] == "u1"
    assert row["skill_name"] == "process-return"
    assert row["outcome"] == "success"
    assert row["tool_calls"] == [{"name": "query_order", "ok": True, "error": None}]
    assert row["created_at"]


def test_list_skill_traces_filters_by_skill_and_outcome(tmp_path):
    db = _db(tmp_path)
    db.record_skill_trace("s1", "u1", "process-return", [], "success")
    db.record_skill_trace("s2", "u1", "process-return", [], "handoff")
    db.record_skill_trace("s3", "u2", "track-order", [], "handoff")

    only_return = db.list_skill_traces(skill_name="process-return")
    assert {r["session_id"] for r in only_return} == {"s1", "s2"}

    failed_return = db.list_skill_traces(
        skill_name="process-return", outcomes=["handoff", "tool_error"]
    )
    assert [r["session_id"] for r in failed_return] == ["s2"]

    all_failed = db.list_skill_traces(outcomes=["handoff"])
    assert {r["session_id"] for r in all_failed} == {"s2", "s3"}


def test_list_skill_traces_newest_first_and_respects_limit(tmp_path):
    db = _db(tmp_path)
    for i in range(3):
        db.record_skill_trace(f"s{i}", "u1", "track-order", [], "success")

    rows = db.list_skill_traces(limit=2)
    assert [r["session_id"] for r in rows] == ["s2", "s1"]   # id DESC


def test_list_skill_traces_skips_bad_json(tmp_path):
    db = _db(tmp_path)
    db.record_skill_trace("good", "u1", "track-order", [{"name": "x", "ok": True, "error": None}], "success")

    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO skill_traces (session_id, user_id, skill_name, tool_calls, "
            "outcome, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("bad", "u1", "track-order", "{not json", "success", "2026-01-01 00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    rows = db.list_skill_traces()
    assert [r["session_id"] for r in rows] == ["good"]   # 坏 JSON 那条被跳过
