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


def test_get_grant_returns_the_row_recording_which_draft_granted_it(db):
    db.grant_coupon("SHOE30", "u1", 7, "r", "admin")
    row = db.get_grant("SHOE30", "u1")
    assert row is not None and row["draft_id"] == 7


def test_get_grant_returns_none_when_no_such_grant(db):
    assert db.get_grant("SHOE30", "u1") is None


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


def test_retry_by_same_draft_after_grant_is_not_treated_as_duplicate(db, monkeypatch):
    """Critical 修复:发券成功、投递失败、店主对**同一条草稿**重试——第二次
    调用 issue_for_draft 必须放行(继续投递),而不是把自己上一次发放成功
    的那一行误判成"重复发放"。"""
    from app.agent.coupons import grants
    monkeypatch.setattr(grants, "get_db", lambda: db)
    draft = {"id": 42, "user_id": "u1", "offer": {"coupon_code": "SHOE30"}}

    ok1, why1 = grants.issue_for_draft(draft, "admin")
    assert ok1 is True and why1 == ""
    assert len(db.list_user_grants("u1")) == 1

    ok2, why2 = grants.issue_for_draft(draft, "admin")   # 模拟同一条草稿的重试
    assert ok2 is True and why2 == ""
    assert len(db.list_user_grants("u1")) == 1   # 没有发第二次


def test_cross_draft_duplicate_is_still_refused(db, monkeypatch):
    """不同草稿撞上同一张券:仍是真正的重复发放,必须拒绝——修复"同一草稿
    可重试"不能连带放宽这条防线。"""
    from app.agent.coupons import grants
    monkeypatch.setattr(grants, "get_db", lambda: db)

    draft_a = {"id": 1, "user_id": "u1", "offer": {"coupon_code": "SHOE30"}}
    ok_a, _ = grants.issue_for_draft(draft_a, "admin")
    assert ok_a is True

    draft_b = {"id": 2, "user_id": "u1", "offer": {"coupon_code": "SHOE30"}}
    ok_b, why_b = grants.issue_for_draft(draft_b, "admin")
    assert ok_b is False and "不能重复发放" in why_b
    assert len(db.list_user_grants("u1")) == 1   # 仍只有草稿 1 发的那一条


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


def test_draft_outreach_schema_advertises_coupon_code_without_enum():
    """营销 Agent 得先在工具 schema 里看得到 coupon_code 才能真的提出券码——
    否则发券这条路径在产品里根本摸不到,只有测试在练它。但绝不能把
    KNOWN_CODES 在 import 时冻结成 schema 的 enum:那正是这个代码库已经栽过
    三次的手抄表漂移——校验的权威永远是 issue_for_draft,在批准那一刻查
    活的券列表,不是这里的一份静态快照。"""
    from app.agent.tools.registry import TOOL_DEFINITIONS
    schema = next(t for t in TOOL_DEFINITIONS
                  if t["function"]["name"] == "draft_outreach")
    props = schema["function"]["parameters"]["properties"]
    assert "coupon_code" in props
    assert props["coupon_code"]["type"] == "string"
    assert "enum" not in props["coupon_code"]


def test_draft_outreach_with_coupon_code_only_writes_draft_row(db, monkeypatch):
    """通过工具真的传 coupon_code 起草,落的仍然只是一行草稿——发放动作
    (grant_coupon)绝不会在起草这一步被触发,只能等审批端点调
    issue_for_draft。"""
    from app.agent.tools import growth
    monkeypatch.setattr(growth, "get_db", lambda: db)
    out = growth.draft_outreach(user_id="u1", content="给您留了一张券",
                                kind="unpaid_order", coupon_code="SHOE30")
    assert out["success"] is True and out["status"] == "draft"
    rows = db.list_outreach_drafts(status="draft")
    assert len(rows) == 1 and rows[0]["offer"]["coupon_code"] == "SHOE30"
    assert db.list_user_grants("u1") == []
