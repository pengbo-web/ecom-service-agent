"""审批与触达 API:鉴权、幂等、发送失败不留悬空状态、驳回不发送。"""

import pytest
from fastapi.testclient import TestClient

# 管理鉴权走 X-Admin-Token(见 app/hardening/auth.py::make_admin_auth),不是
# Authorization: Bearer —— 与 tests/test_seller_api.py、tests/test_skill_admin_api.py
# 等既有 admin 端点测试用的头一致。
AUTH = {"X-Admin-Token": "T"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")

    # 隔离数据库:get_db() 默认是进程内单例、指向真实的 app/sessions/ecom.db,
    # 这张本地开发用 scratch 库会跨多次运行累积 outreach_drafts 行。不隔离的话,
    # test_list_drafts 这类精确断言会被"上一次运行/别的会话留下的行"污染
    # (报告里记录过一次真实的失败)。改成每个测试各自一份 tmp_path 下的临时
    # sqlite 文件,测试结束后把单例复位,不影响同进程里其它测试文件。
    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "growth_api_test.db"))
    db.init_schema()
    set_db(db)

    from app.api.app import create_app
    c = TestClient(create_app())
    yield c
    set_db(None)


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
    # 投递函数挂在**这个 app 实例**的 app.state 上(见 app/api/app.py
    # create_app() 内的注释),而不是模块级名字——两个 create_app() 出来的
    # app 各自持有自己的 deliver_outreach,互不覆盖。打桩就打在这个实例上。
    sent = []
    monkeypatch.setattr(client.app.state, "deliver_outreach",
                        lambda d: sent.append(d["id"]) or True)
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.status_code == 200 and r.json()["sent"] is True
    assert sent == [draft]


def test_second_approve_is_a_no_op(client, draft, monkeypatch):
    """连点两次批准不能给同一个买家发两遍。"""
    sent = []
    monkeypatch.setattr(client.app.state, "deliver_outreach",
                        lambda d: sent.append(d["id"]) or True)
    client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    r2 = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r2.json()["sent"] is False
    assert sent == [draft]


def test_delivery_failure_does_not_leave_dangling_approved(client, draft, monkeypatch):
    """发送失败时不能停在"已批准但没发"的悬空态,必须可重试。"""
    from app.db import get_db
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda d: False)
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.json()["sent"] is False
    assert get_db().get_outreach_draft(draft)["status"] == "draft"   # 退回可重试


def test_delivery_failure_revert_error_returns_actionable_response(client, draft, monkeypatch):
    """退回待审状态这一步本身出错时,不能裸 500,更不能悄悄悬停在 approved——
    调用方必须拿到一个明确指出"需人工核查"且带着草稿 id 的响应。"""
    from app.db import get_db
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda d: False)

    def _boom(draft_id):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(get_db(), "revert_outreach_to_pending", _boom)
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.status_code == 200   # 不是没有信息量的裸 500
    body = r.json()
    assert body["success"] is False
    assert body["sent"] is False
    assert str(draft) in body["reason"]
    assert "人工" in body["reason"]


def test_send_success_but_mark_sent_failure_is_not_reported_as_clean(client, draft, monkeypatch):
    """消息已经真实投递给买家,但落库标记"已发送"没生效时,不能谎报一次
    干净的成功——mark_outreach_sent 的返回值和 review_outreach_draft 一样
    要被认真对待。"""
    from app.db import get_db
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda d: True)
    monkeypatch.setattr(get_db(), "mark_outreach_sent", lambda draft_id: False)
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    body = r.json()
    assert body["sent"] is True       # 消息确已投递,不可撤销,不能说没发
    assert body["success"] is False   # 但账本没对上,不能算"干净成功"
    assert str(draft) in body["reason"]


def test_reject_never_sends(client, draft, monkeypatch):
    from app.db import get_db
    sent = []
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda d: sent.append(1) or True)
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
