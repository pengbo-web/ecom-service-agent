"""审批与触达 API:鉴权、幂等、发送失败不留悬空状态、驳回不发送。"""

import pytest
from fastapi.testclient import TestClient

# 管理鉴权走 X-Admin-Token(见 app/hardening/auth.py::make_admin_auth),不是
# Authorization: Bearer —— 与 tests/test_seller_api.py、tests/test_skill_admin_api.py
# 等既有 admin 端点测试用的头一致。
AUTH = {"X-Admin-Token": "T"}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")
    from app.api.app import create_app
    return TestClient(create_app())


@pytest.fixture()
def draft(client):
    from app.db import get_db
    return get_db().create_outreach_draft(
        "stale_pending_order", "u1", "O1", "这单还差一步", {}, "未付款", "C1", "growth")


def test_requires_auth(client):
    assert client.get("/api/admin/growth/drafts").status_code in (401, 403)


def test_list_drafts(client, draft):
    r = client.get("/api/admin/growth/drafts?status=draft", headers=AUTH)
    assert r.status_code == 200
    assert [d["id"] for d in r.json()["drafts"]] == [draft]


def test_approve_sends_once(client, draft, monkeypatch):
    from app.api import app as appmod
    sent = []
    monkeypatch.setattr(appmod, "_deliver_outreach", lambda d: sent.append(d["id"]) or True)
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.status_code == 200 and r.json()["sent"] is True
    assert sent == [draft]


def test_second_approve_is_a_no_op(client, draft, monkeypatch):
    """连点两次批准不能给同一个买家发两遍。"""
    from app.api import app as appmod
    sent = []
    monkeypatch.setattr(appmod, "_deliver_outreach", lambda d: sent.append(d["id"]) or True)
    client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    r2 = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r2.json()["sent"] is False
    assert sent == [draft]


def test_delivery_failure_does_not_leave_dangling_approved(client, draft, monkeypatch):
    """发送失败时不能停在"已批准但没发"的悬空态,必须可重试。"""
    from app.api import app as appmod
    from app.db import get_db
    monkeypatch.setattr(appmod, "_deliver_outreach", lambda d: False)
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.json()["sent"] is False
    assert get_db().get_outreach_draft(draft)["status"] == "draft"   # 退回可重试


def test_reject_never_sends(client, draft, monkeypatch):
    from app.api import app as appmod
    from app.db import get_db
    sent = []
    monkeypatch.setattr(appmod, "_deliver_outreach", lambda d: sent.append(1) or True)
    r = client.post(f"/api/admin/growth/drafts/{draft}/reject", headers=AUTH)
    assert r.status_code == 200
    assert sent == []
    assert get_db().get_outreach_draft(draft)["status"] == "rejected"


def test_missing_draft_is_404(client):
    assert client.post("/api/admin/growth/drafts/99999/approve",
                       headers=AUTH).status_code == 404


def test_opportunities_endpoint(client):
    r = client.get("/api/admin/growth/opportunities?kind=stale_pending_order", headers=AUTH)
    assert r.status_code == 200 and r.json()["success"] is True


def test_unknown_kind_is_400(client):
    assert client.get("/api/admin/growth/opportunities?kind=zzz",
                      headers=AUTH).status_code == 400
