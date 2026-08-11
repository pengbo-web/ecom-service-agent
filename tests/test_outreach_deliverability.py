"""不可达的买家:批准之前要说清楚,失败之后不许说"可重试"。

**实测缺陷**(走查协作链后半段时抓到)。给 `walkthrough_buyer` 造了订单(直接写库,
从没聊过天),商机发现器照常挑出他 → 营销花一次 LLM 起草 → 人工审 → 点批准 →

    {"success": false, "sent": false, "reason": "投递失败,已退回待审,可重试"}

**重试永远失败。** 投递通道是"把消息追加进买家自己的客服会话"
(`_deliver_outreach`),买家没有会话就没有可追加的地方,再点一次结果一样。而
`_deliver_outreach` 把这种永久性失败与"抢不到锁"、"落盘失败"塌成同一个
`delivered=False`,于是操作者一律被告知可重试,在结构上不可达的目标上反复消耗人工
注意力。

值得注意的是 `_revert_after_failure` **本来就有** `retryable` 参数,它的 docstring
也写着"不能不管三七二十一都说可重试"——发券失败那两种情形正确地用了
`retryable=False`,只有投递这一路被硬编码成 True,这一种情形从那条纪律里漏了出去。

另一半:商机发现器读 orders/carts,与 conversations 无关,所以待审队列里本来就会
混进不可达的目标。**批准之前**就该标出来,与"这条草稿会真的发一张券"是同一条原则
(见 growth_drafts 端点里 coupon_discount 那段注释)。
"""

import pytest
from fastapi.testclient import TestClient

# 管理鉴权走 X-Admin-Token(与 tests/test_growth_api.py 等既有 admin 端点测试一致)。
AUTH = {"X-Admin-Token": "T"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """隔离库 + 管理鉴权。口径照 tests/test_growth_api.py 那份:get_db() 默认是
    进程内单例、指向真实的 app/sessions/ecom.db,不隔离会被历史行污染。"""
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")

    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "deliverability_test.db"))
    db.init_schema()
    set_db(db)

    from app.api.app import create_app
    c = TestClient(create_app())
    yield c
    set_db(None)


def _make_draft(user_id: str = "no_session_buyer") -> int:
    from app.db import get_db
    return get_db().create_outreach_draft(
        "stale_pending_order", user_id, "O-1", "您好呀～您这单还在待发货", {},
        "下单后久未推进 · 滞留 212h", "C-1", "growth")


# --------------------------------------------------------------------------
# 归一化:老式返回值行为逐字节不变
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expect_retry_wording", [
    # 老式裸 bool:没有"性质"这一档,必须仍按可重试处理
    (False, True),
    # 不带新键的字典(既有测试里的桩就是这个形状)
    ({"delivered": False}, True),
    # 显式标注永久性失败
    ({"delivered": False, "reason": "该买家没有客服会话", "retryable": False}, False),
    # 带 reason 但没说性质:缺省仍是可重试,不擅自升级成永久失败
    ({"delivered": False, "reason": "追加回复落盘失败"}, True),
])
def test_delivery_failure_wording_follows_the_channel(client, monkeypatch,
                                                     raw, expect_retry_wording):
    """失败文案的"可重试"必须跟着投递函数给的性质走,不在端点里猜。

    这条同时是**兼容性契约**:`app.state.deliver_outreach` 是可替换注入点,历史签名
    返回裸 bool。裸 bool 与不带 retryable 键的字典都必须仍被当成"可重试",否则这次
    改动会把既有投递通道(和所有拿它打桩的测试)的语义悄悄改掉。
    """
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda draft: raw)
    draft_id = _make_draft("has_session_buyer")
    body = client.post(f"/api/admin/growth/drafts/{draft_id}/approve",
                       headers=AUTH).json()
    assert body["sent"] is False
    if expect_retry_wording:
        assert "可重试" in body["reason"], f"暂时性失败应说可重试: {body['reason']}"
    else:
        assert "可重试" not in body["reason"]
        assert "重试不会成功" in body["reason"]


def test_unreachable_buyer_is_not_advertised_as_retryable(client):
    """核心断言:永久性失败不许说"可重试"。"""
    draft_id = _make_draft()
    r = client.post(f"/api/admin/growth/drafts/{draft_id}/approve", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert body["sent"] is False
    assert "没有客服会话" in body["reason"], f"原因没说清楚: {body['reason']}"
    assert "可重试" not in body["reason"], (
        f"这条重试永远失败,不能告诉操作者可重试: {body['reason']}")
    assert "重试不会成功" in body["reason"]


def test_unreachable_draft_returns_to_pending_for_human_handling(client):
    """仍要退回待审:不能停在"已批准但没发"的悬空态,店主要能看到并处置。"""
    from app.db import get_db
    draft_id = _make_draft()
    client.post(f"/api/admin/growth/drafts/{draft_id}/approve", headers=AUTH)
    assert get_db().get_outreach_draft(draft_id)["status"] == "draft"


def test_drafts_listing_flags_unreachable_before_approval(client):
    """**批准之前**就要标出来——这是这条修复真正省下人工注意力的地方。"""
    _make_draft()
    rows = client.get("/api/admin/growth/drafts", headers=AUTH).json()["drafts"]
    assert rows
    d = rows[0]
    assert d["deliverable"] is False
    assert "没有客服会话" in d["undeliverable_reason"]
    assert "重试" in d["undeliverable_reason"]


def test_drafts_listing_marks_reachable_buyer_as_deliverable(client):
    """有会话的买家不该被误标:这道标注不能变成"人人都红"的噪音。"""
    from app.db import get_db
    db = get_db()
    db.ensure_conversation("c-1", "has_session_buyer") if hasattr(
        db, "ensure_conversation") else None
    # 用与生产同一条写路径建会话:没有该方法时退回直接插行,断言仍然成立
    if not hasattr(db, "ensure_conversation"):
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO conversations (conversation_id, user_id, status, created_at) "
                "VALUES ('c-1', 'has_session_buyer', 'open', datetime('now'))")
            conn.commit()
        finally:
            conn.close()
    _make_draft("has_session_buyer")
    rows = client.get("/api/admin/growth/drafts", headers=AUTH).json()["drafts"]
    row = next(r for r in rows if r["user_id"] == "has_session_buyer")
    assert row["deliverable"] is True
    assert row["undeliverable_reason"] == ""


def test_listing_does_not_filter_unreachable_drafts(client):
    """**只标注,不过滤。** 买家没有客服会话不等于这条商机不成立(店铺可能有别的
    触达渠道,买家也可能明天就来咨询),悄悄丢掉是另一种错——而且是更难发现的那种。"""
    _make_draft("no_session_buyer")
    rows = client.get("/api/admin/growth/drafts", headers=AUTH).json()["drafts"]
    assert any(r["user_id"] == "no_session_buyer" for r in rows), (
        "不可达的草稿被过滤掉了:店主再也看不到这条商机存在过")
