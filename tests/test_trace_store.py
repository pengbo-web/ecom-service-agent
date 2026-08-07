import sqlite3

from app.observability.trace import Trace, Span
from app.observability.store import TraceStore


def _make_trace():
    spans = [
        Span("s1", "t1", "llm.chat.create", "llm", 1.0, 1.5, 500.0, None, 100, 20, {}),
        Span("s2", "t1", "tool:query_order", "tool", 1.5, 1.6, 100.0, True, 0, 0, {"name": "query_order"}),
    ]
    return Trace("t1", "sess1", "查订单", "order_query", 1.0, 1.7, 700.0, "ok", None, spans)


def test_trace_token_sums():
    t = _make_trace()
    assert t.prompt_tokens == 100
    assert t.completion_tokens == 20


def test_save_and_get_trace(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    store.save_trace(_make_trace())

    got = store.get_trace("t1")
    assert got["trace_id"] == "t1"
    assert got["intent"] == "order_query"
    assert len(got["spans"]) == 2
    assert got["prompt_tokens"] == 100


def test_recent_traces(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    store.save_trace(_make_trace())
    rows = store.recent_traces(limit=10)
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "t1"
    assert rows[0]["status"] == "ok"


def test_span_parent_span_id_round_trips(tmp_path):
    """W1:stage 嵌套树的父指针要能完整落库/读出——这是前端重建嵌套关系
    的唯一依据。既有字段(kind/name/latency 等)保持不变，parent_span_id
    是纯新增列，旧数据(该列为 NULL)读出来是 None，不是缺 key 抛异常。"""
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    spans = [
        Span("root", "t1", "stage:react", "stage", 0.0, 1.0, 1000.0),
        Span("child", "t1", "tool:query_order", "tool", 0.2, 0.5, 300.0,
             True, 0, 0, {}, parent_span_id="root"),
    ]
    store.save_trace(Trace("t1", "sess1", "查订单", "order_query",
                            0.0, 1.0, 1000.0, "ok", None, spans))

    got = store.get_trace("t1")
    by_id = {s["span_id"]: s for s in got["spans"]}
    assert by_id["root"]["parent_span_id"] is None
    assert by_id["child"]["parent_span_id"] == "root"


def test_init_schema_migrates_old_db_missing_parent_span_id(tmp_path):
    """老库(升级前建的表,没有 parent_span_id 列)升级后 init_schema() 要能
    补上这一列而不炸——CREATE TABLE IF NOT EXISTS 对已存在的表是空操作，
    全靠 ALTER TABLE 兜底迁移。"""
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE traces (
            trace_id TEXT PRIMARY KEY, session_id TEXT, user_input TEXT,
            intent TEXT, started_at REAL, ended_at REAL, latency_ms REAL,
            status TEXT, error TEXT, prompt_tokens INTEGER, completion_tokens INTEGER
        );
        CREATE TABLE spans (
            span_id TEXT PRIMARY KEY, trace_id TEXT, name TEXT, kind TEXT,
            started_at REAL, ended_at REAL, latency_ms REAL, success INTEGER,
            prompt_tokens INTEGER, completion_tokens INTEGER, meta TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    store = TraceStore(db_path)
    store.init_schema()   # 不应抛异常
    store.save_trace(Trace(
        "t1", "sess1", "老库迁移", None, 0.0, 0.1, 100.0, "ok", None,
        [Span("s1", "t1", "stage:react", "stage", 0.0, 0.1, 100.0,
              parent_span_id=None)],
    ))
    got = store.get_trace("t1")
    assert got["spans"][0]["parent_span_id"] is None


def test_recent_traces_filter_by_session(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    store.save_trace(Trace("t1", "sessA", "a", "greeting", 1.0, 1.1, 100.0, "ok", None, []))
    store.save_trace(Trace("t2", "sessB", "b", "order_query", 2.0, 2.1, 100.0, "ok", None, []))
    only_a = store.recent_traces(limit=10, session_id="sessA")
    assert len(only_a) == 1
    assert only_a[0]["trace_id"] == "t1"
    assert len(store.recent_traces(limit=10)) == 2
