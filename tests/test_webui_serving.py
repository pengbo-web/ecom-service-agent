from fastapi.testclient import TestClient
from app.api.app import create_app


def test_root_serves_spa():
    app = create_app()
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert "<div id=\"root\">" in r.text or "小夕" in r.text


def test_dashboard_serves_spa():
    app = create_app()
    c = TestClient(app)
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "<div id=\"root\">" in r.text or "小夕" in r.text


def test_health_still_ok():
    app = create_app()
    c = TestClient(app)
    assert c.get("/api/health").json() == {"status": "ok"}
