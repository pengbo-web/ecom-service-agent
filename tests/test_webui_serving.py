from fastapi.testclient import TestClient
from app.api.app import create_app


def test_root_serves_spa_or_legacy():
    app = create_app()
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert "<div id=\"root\">" in r.text or "小夕" in r.text


def test_legacy_serves_old_page():
    app = create_app()
    c = TestClient(app)
    r = c.get("/legacy")
    assert r.status_code == 200
    assert "switchTab" in r.text


def test_health_still_ok():
    app = create_app()
    c = TestClient(app)
    assert c.get("/api/health").json() == {"status": "ok"}
