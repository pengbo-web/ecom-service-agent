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


def test_dashboard_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
