"""发券:只能由审批端点触发、券码必须已知、幂等、失败不投递。"""

import pytest

from app.db.database import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    return d


def test_grant_roundtrip(db):
    gid = db.grant_coupon("SHOE30", "u1", 1, "尺码问题补偿", "admin")
    assert gid and db.list_user_grants("u1")[0]["code"] == "SHOE30"


def test_duplicate_grant_returns_none(db):
    assert db.grant_coupon("SHOE30", "u1", 1, "r", "admin") is not None
    assert db.grant_coupon("SHOE30", "u1", 2, "r", "admin") is None


def test_unknown_code_is_refused(db):
    from app.agent.coupons.grants import issue_for_draft
    ok, why = issue_for_draft({"id": 1, "user_id": "u1",
                               "offer": {"coupon_code": "MODEL_MADE_THIS_UP"}}, "admin")
    assert ok is False and "券码" in why


def test_known_codes_derive_from_single_source():
    """券定义只有一处,不许再抄一份。"""
    from app.agent.coupons.grants import KNOWN_CODES
    from app.agent.tools.order_ops import _COUPONS
    assert KNOWN_CODES == {c["code"] for c in _COUPONS}


def test_draft_without_coupon_is_a_noop(db):
    from app.agent.coupons.grants import issue_for_draft
    ok, why = issue_for_draft({"id": 1, "user_id": "u1", "offer": {}}, "admin")
    assert ok is True and why == ""


def test_draft_outreach_never_grants(db, monkeypatch):
    """营销 Agent 只能把券写进草稿,不能发出去。"""
    from app.agent.tools import growth
    monkeypatch.setattr(growth, "get_db", lambda: db)
    growth.draft_outreach(user_id="u1", content="给您一张券", kind="unpaid_order",
                          offer_note="SHOE30")
    assert db.list_user_grants("u1") == []


def test_query_coupons_shows_granted(db, monkeypatch):
    from app.agent.tools import order_ops
    monkeypatch.setattr(order_ops, "get_db", lambda: db)
    db.grant_coupon("SHOE30", "u1", 1, "r", "admin")
    from app.agent.runtime_context import set_current_user
    set_current_user("u1")
    out = order_ops.query_coupons()
    assert any(c.get("code") == "SHOE30" for c in out.get("granted", []))
