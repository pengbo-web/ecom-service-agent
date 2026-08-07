from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.observability.store import TraceStore
from app.observability.trace import Trace, Span


def _client(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    store.save_trace(Trace(
        "t1", "s", "查订单", "order_query", 0.0, 0.2, 200.0, "ok", None,
        [Span("a", "t1", "tool:query_order", "tool", 0, 0.1, 100, True, 0, 0, {})],
    ))
    mgr = SessionManager(agent_factory=lambda p, u=None: None)
    app = create_app(session_manager=mgr, trace_store=store)
    return TestClient(app)


def test_metrics_endpoint(tmp_path):
    c = _client(tmp_path)
    r = c.get("/api/metrics")
    assert r.status_code == 200
    assert r.json()["total_traces"] == 1


def test_traces_endpoints(tmp_path):
    c = _client(tmp_path)
    lst = c.get("/api/traces").json()
    assert len(lst) == 1
    detail = c.get(f"/api/traces/{lst[0]['trace_id']}").json()
    assert detail["trace_id"] == "t1"
    assert len(detail["spans"]) == 1


def test_trace_detail_surfaces_nested_stage_spans_additively(tmp_path):
    """W1:GET /api/traces/{id} 不改既有响应形状——新增的 stage 嵌套 span 只是
    spans 列表里多了几条、多了一个 parent_span_id 键，既有键(trace_id/
    user_input/intent/status/spans[].kind 等)原样不动，前端不需要任何
    schema 迁移就能拿到嵌套信息。"""
    store = TraceStore(str(tmp_path / "tr2.db"))
    store.init_schema()
    store.save_trace(Trace(
        "t2", "s", "退款流程", "after_sale", 0.0, 0.5, 500.0, "ok", None,
        [
            Span("root", "t2", "stage:react", "stage", 0.0, 0.4, 400.0),
            Span("child", "t2", "workflow_guard:refund", "workflow_guard",
                 0.1, 0.1, 0.0, meta={"name": "refund", "reason": "需先查单"},
                 parent_span_id="root"),
        ],
    ))
    mgr = SessionManager(agent_factory=lambda p, u=None: None)
    app = create_app(session_manager=mgr, trace_store=store)
    c = TestClient(app)

    detail = c.get("/api/traces/t2").json()
    assert detail["trace_id"] == "t2"          # 既有键原样不动
    assert detail["status"] == "ok"
    by_id = {s["span_id"]: s for s in detail["spans"]}
    assert by_id["root"]["parent_span_id"] is None
    assert by_id["child"]["parent_span_id"] == "root"
    assert by_id["child"]["kind"] == "workflow_guard"   # 安全标记独立 kind，前端可识别


def test_dashboard_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
