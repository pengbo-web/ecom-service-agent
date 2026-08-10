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


def test_approve_starts_followup_chain(client, draft, monkeypatch):
    """N7 接线回归:投递成功后必须开一条跟进链——「持续沟通」的唯一起点。

    这条曾经是断的:`start_followup` 全仓库只有测试在调,`app/` 里零调用点,
    于是 outreach_followups 表在生产中恒空,整套跟进机制(终止条件/worker/
    控制台面板/11 个测试)全部空转。没有任何测试发现,因为每一块单看都是对的,
    断的是**它们之间那一根线**。
    """
    from app.db import get_db
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda d: True)

    assert get_db().due_followups(limit=10) == []          # 批准前:没有任何链
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.json()["sent"] is True

    chain = get_db().active_followup("u1", "stale_pending_order")
    assert chain is not None, "投递成功后没有开跟进链(N7 接线又断了)"
    assert chain["status"] == "active" and chain["step"] == 1
    # 链必须复用草稿的 correlation_id:followup._last_touch_converted 正是按它
    # 去 outreach_drafts 里找"上一次触达判没判成 converted",对不上就永远判不出。
    assert chain["correlation_id"] == "C1"


def test_approve_does_not_start_second_chain_for_same_user_and_kind(client, monkeypatch):
    """一人一类型只有一条链:同类型的第二条草稿被批准时不新建链,也不报错。"""
    from app.db import get_db
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda d: True)
    db = get_db()
    d1 = db.create_outreach_draft("unpaid_order", "u9", "O1", "催一下", {}, "", "CA", "growth")
    d2 = db.create_outreach_draft("unpaid_order", "u9", "O2", "再催一下", {}, "", "CB", "growth")

    assert client.post(f"/api/admin/growth/drafts/{d1}/approve", headers=AUTH).json()["sent"]
    r2 = client.post(f"/api/admin/growth/drafts/{d2}/approve", headers=AUTH)
    assert r2.json()["sent"] is True        # 第二条消息照发,起链失败不影响投递
    chain = db.active_followup("u9", "unpaid_order")
    assert chain["correlation_id"] == "CA"  # 仍是第一条链,没有被顶掉


def test_followup_start_failure_never_blocks_delivery(client, draft, monkeypatch):
    """起链失败不得推翻"消息已经真实投递"这个不可撤销的事实。"""
    from app.db import get_db
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda d: True)

    def _boom(*a, **k):
        raise RuntimeError("followup table unavailable")

    monkeypatch.setattr(get_db(), "start_followup", _boom)
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.json()["sent"] is True and r.json()["success"] is True
    assert get_db().get_outreach_draft(draft)["status"] == "sent"


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


def test_mark_sent_failure_leaves_no_dangling_baseline(client, draft, monkeypatch):
    """review finding 4:`mark_outreach_sent` 返回 False 时(草稿留在
    approved,进不了只认 status='sent' 的 pending_attribution/outreach_stats),
    绝不能还带着一条看似正常的归因基线——那样的草稿会比"没有基线"更迷惑,
    因为乍看像是已经正常记过账。基线只应该在草稿真的翻成 sent 之后才写。"""
    from app.db import get_db
    db = get_db()
    monkeypatch.setattr(client.app.state, "deliver_outreach", lambda d: True)
    monkeypatch.setattr(db, "mark_outreach_sent", lambda draft_id: False)
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    body = r.json()
    assert body["sent"] is True and body["success"] is False   # 既有口径不变
    row = db.get_outreach_draft(draft)
    assert row["status"] == "approved"          # 今天状态机下"不可达"但要有定论
    assert row["status_at_send"] is None         # 没有被写过基线,不是空字符串


# ---------------------------------------------------------------------------
# 真实投递路径(不打桩 deliver_outreach):把假的会话 agent 注入 SessionManager,
# 让 _deliver_outreach 真的走完 latest_conversation → session_lock →
# _append_agent_reply → touch_conversation 这一串。
# ---------------------------------------------------------------------------

class _FakeAgent:
    """最小的会话 agent:只需要 raw_messages 与 save()——这正是
    `_append_agent_reply` 唯一依赖的两样东西。"""

    def __init__(self, save_error: Exception | None = None):
        self.raw_messages: list = []
        self._save_error = save_error
        self.saves = 0

    def save(self):
        self.saves += 1
        if self._save_error is not None:
            raise self._save_error


@pytest.fixture()
def real_delivery(monkeypatch, tmp_path):
    """返回 (client, agent, draft_id, db):投递走真实实现,买家会话是 _FakeAgent。"""
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")

    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "growth_delivery_test.db"))
    db.init_schema()
    set_db(db)

    agent = _FakeAgent()

    def _factory(session_path, user_id=None):
        return agent

    from app.api.app import create_app
    from app.api.session_manager import SessionManager
    mgr = SessionManager(agent_factory=_factory, base_dir=str(tmp_path / "sessions"))
    c = TestClient(create_app(session_manager=mgr))

    conv = db.create_conversation("u1")
    did = db.create_outreach_draft("stale_pending_order", "u1", "O1",
                                   "这单还差一步", {}, "久未推进", "C1", "growth")
    yield c, agent, did, db, conv["conversation_id"]
    set_db(None)


def test_real_delivery_appends_into_buyer_session(real_delivery):
    """先钉住基准:不注入任何故障时,真实投递确实把消息写进买家会话并落盘。

    没有这条,下面两条故障注入测试可能是在"根本没走到那一步"的情况下通过的。
    """
    c, agent, did, db, _sid = real_delivery
    r = c.post(f"/api/admin/growth/drafts/{did}/approve", headers=AUTH)
    assert r.json() == {"success": True, "sent": True, "reason": ""}
    assert len(agent.raw_messages) == 1 and agent.saves == 1
    assert "这单还差一步" in agent.raw_messages[0]["content"]
    assert db.get_outreach_draft(did)["status"] == "sent"


def test_failure_after_append_is_not_reported_as_retryable(real_delivery, monkeypatch):
    """【C1】消息追加成功之后的任何一步失败,都不得把这次投递变成"可重试的失败"。

    真实故障场景:`touch_conversation` 写的是协作 worker 也在写的同一个 SQLite
    文件,撞上默认 5 秒 busy timeout 就抛 OperationalError。修复前它被
    `_deliver_outreach` 最外层的 except 抓住 → 返回 False → approve_draft 把草稿
    退回 draft"以便重试" → 店主一重试,同一个买家的会话里就多了**第二条**一模
    一样的消息。分支的承诺是"没有人工不发,发也绝不发两遍"。

    所以这里同时钉三件事:
      ① 买家会话里始终只有一条消息(重试不会产生第二条,因为压根不该退回);
      ② 草稿没有回到 draft 这种可重试状态;
      ③ 操作者被明确告知"已投递",而不是"投递失败"。
    """
    import sqlite3
    c, agent, did, db, _sid = real_delivery

    def _busy(_cid):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "touch_conversation", _busy)

    r = c.post(f"/api/admin/growth/drafts/{did}/approve", headers=AUTH)
    body = r.json()

    assert len(agent.raw_messages) == 1        # ① 消息确实投出去了,且只有一条
    assert body["sent"] is True                # ③ 如实告知:已投递,不可撤销
    assert body["success"] is False            # 但不是"干净的成功",有事要看
    assert "重试" in body["reason"]             # 明确写着别重试(记账告警)
    # ② 绝不能回到可重试状态,否则店主的重试会发第二条
    status = db.get_outreach_draft(did)["status"]
    assert status != "draft", "投递已发生却把草稿退回待审 = 重试会给买家发第二条"
    assert status == "sent"


def test_exception_while_releasing_the_lock_is_also_not_retryable(real_delivery,
                                                                  monkeypatch):
    """同一条纪律的第二个现场:异常发生在**追加之后、但仍在 try 内**。

    `with session_lock.guard(sid)` 的退出(Redis 后端释放锁)也可能抛,而它就在
    最外层 try 里、在追加之后。所以"把危险语句摆到 try 之外"不足以保证纪律——
    只对当时想到的那一句有效。真正的保证是那个 `appended` 标志:一旦置位,
    任何出口都必须报 delivered=True。
    """
    c, agent, did, db, _sid = real_delivery
    from app.api import app as app_mod

    real_guard = app_mod.get_session_lock().guard

    class _BoomOnExit:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, *exc):
            self._inner.__exit__(*exc)
            raise RuntimeError("释放锁失败")

    monkeypatch.setattr(app_mod.get_session_lock(), "guard",
                        lambda sid: _BoomOnExit(real_guard(sid)))

    body = c.post(f"/api/admin/growth/drafts/{did}/approve", headers=AUTH).json()
    assert len(agent.raw_messages) == 1     # 消息确实进了买家会话
    assert body["sent"] is True             # 就必须如实说"已投递"
    assert "重试" in body["reason"]
    assert db.get_outreach_draft(did)["status"] == "sent"


def test_failed_persist_on_outreach_path_never_ends_as_sent(monkeypatch, tmp_path):
    """【C2】触达路径上落盘失败时,绝不能以草稿被标成 sent 收场。

    修复前:`_append_agent_reply` 把 `agent.save()` 包在裸 `except: pass` 里并
    无条件返回 True,于是消息只活在内存里的 agent 上;approve_draft 照样把行
    翻成 sent,店主看到一次干净的成功。等 idle reaper 淘汰会话或进程重启,这条
    消息就没了——而 `review_outreach_draft` 只认还在 draft 的行,这条草稿再也
    重试不了。静默的永久丢失。

    修复后:落盘失败 = 投递失败,草稿退回 draft(**可**重试),并且内存里那条
    半截的追加被撤回,所以重试不会变成发两遍。
    """
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")

    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "growth_persist_test.db"))
    db.init_schema()
    set_db(db)
    try:
        agent = _FakeAgent(save_error=OSError("disk full"))
        from app.api.app import create_app
        from app.api.session_manager import SessionManager
        mgr = SessionManager(agent_factory=lambda p, u=None: agent,
                             base_dir=str(tmp_path / "sessions"))
        c = TestClient(create_app(session_manager=mgr))
        db.create_conversation("u1")
        did = db.create_outreach_draft("stale_pending_order", "u1", "O1", "x", {},
                                       "r", "C1", "growth")

        r = c.post(f"/api/admin/growth/drafts/{did}/approve", headers=AUTH)
        body = r.json()

        assert body["sent"] is False
        assert db.get_outreach_draft(did)["status"] == "draft"   # 可重试,没丢
        assert db.get_outreach_draft(did)["status"] != "sent"
        # 撤回内存里的半截追加:否则重试就会在买家会话里留下两条
        assert agent.raw_messages == []
    finally:
        set_db(None)


def test_admin_reply_keeps_tolerating_save_failure(monkeypatch, tmp_path):
    """坐席人工回复端点的容忍度**必须原样保留**:落盘失败照旧返回 200 + 气泡。

    那条路径上的吞错是可接受的——响应体里就带着重建后的气泡流,坐席当场看得见
    自己那条回复有没有进去,而且他人还在屏幕前。C2 的收紧只针对无人值守的触达
    路径,不能顺手改掉这里(会改变一个既有端点的对外行为)。
    """
    monkeypatch.setenv("ADMIN_TOKEN", "T")
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "admin_token", "T")

    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "reply_tolerance_test.db"))
    db.init_schema()
    set_db(db)
    try:
        agent = _FakeAgent(save_error=OSError("disk full"))
        from app.api.app import create_app
        from app.api.session_manager import SessionManager
        mgr = SessionManager(agent_factory=lambda p, u=None: agent,
                             base_dir=str(tmp_path / "sessions"))
        c = TestClient(create_app(session_manager=mgr))
        sid = db.create_conversation("u1")["conversation_id"]

        r = c.post(f"/api/admin/session/{sid}/reply", json={"text": "在的"}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        # 落盘炸了,但消息照旧留在内存并回进气泡流(既有行为,逐字不变)
        assert len(agent.raw_messages) == 1
        assert r.json()["turns"], "坐席应能在响应里看到自己刚发的那条"
    finally:
        set_db(None)


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


def test_opportunity_kinds_endpoint_matches_backend_source_of_truth(client):
    """商机类型全集端点必须原样反映 OPPORTUNITY_KINDS——前端「商机概览」全靠
    这个端点驱动,不再自己抄一份 kind→label 表。新增一个 kind 只改
    growth.py 这一处,这条测试就应该跟着长出新的一条。"""
    from app.agent.tools.growth import OPPORTUNITY_KINDS
    r = client.get("/api/admin/growth/opportunity-kinds", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert [(k["kind"], k["label"]) for k in body["kinds"]] == list(OPPORTUNITY_KINDS.items())
    # 两个最新分类必须出现在里面,不能被遗漏
    kinds = {k["kind"] for k in body["kinds"]}
    assert "unpaid_order" in kinds
    assert "abandoned_cart" in kinds


def test_opportunity_kinds_requires_auth(client):
    assert client.get("/api/admin/growth/opportunity-kinds").status_code in (401, 403)


def test_approve_blocked_when_buyer_in_manual_takeover(client, draft, monkeypatch):
    """端点级:仲裁拒绝时不投递、不改草稿状态,草稿仍留在待审列表可重试。

    注:投递函数挂在这个 app 实例的 app.state 上(与本文件其它测试一致,
    见 test_approve_sends_once 的注释),而非模块级的 `_deliver_outreach`
    ——后者是 create_app() 内部的闭包函数,app.api.app 模块本身没有这个名字。

    同时钉住新增的 `block_code` 字段(review finding 3):仲裁拒绝时响应体
    在既有的 success/sent/reason 之外附带机器可读的拒绝类型,前端/日志不必
    再靠中文文案区分"人工接管"与"未结工单"与"查不清状态"。
    """
    from app.db import get_db
    from app.multi_agent import arbitration as arb
    sent = []
    monkeypatch.setattr(client.app.state, "deliver_outreach",
                        lambda d: sent.append(1) or True)
    monkeypatch.setattr("app.multi_agent.arbitration.check_outreach_allowed",
                        lambda user_id, hitl=None, db=None:
                            (False, arb.BLOCK_MANUAL, "该买家的会话正由人工客服接管中"))
    r = client.post(f"/api/admin/growth/drafts/{draft}/approve", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False
    assert "人工" in body["reason"]
    assert body["block_code"] == arb.BLOCK_MANUAL                # 新增字段,附加不替换
    assert sent == []                                        # 没投递
    assert get_db().get_outreach_draft(draft)["status"] == "draft"   # 状态没被消耗


# ---------------------------------------------------------------------------
# 发券(N6 review finding):走真实审批端点,覆盖"发券成功但投递失败后重试"
# 这条此前只被单元测试(直接调 issue_for_draft)绕过、从未被端到端练过的死循环。
# ---------------------------------------------------------------------------

@pytest.fixture()
def coupon_draft(client):
    """带一张真实在售券(SHOE30,audience=all,任何买家都能领)的待审草稿。"""
    from app.db import get_db
    return get_db().create_outreach_draft(
        "stale_pending_order", "u1", "O1", "这单还差一步,送您一张鞋类券",
        {"coupon_code": "SHOE30"}, "未付款", "C1", "growth")


@pytest.fixture()
def unknown_coupon_draft(client):
    """券码是模型编出来的,不在 order_ops._COUPONS 里。"""
    from app.db import get_db
    return get_db().create_outreach_draft(
        "stale_pending_order", "u1", "O1", "送您一张券",
        {"coupon_code": "MODEL_MADE_THIS_UP"}, "未付款", "C1", "growth")


def test_approve_with_coupon_grants_and_delivers(client, coupon_draft, monkeypatch):
    """happy path:带券的草稿一次批准成功,券真的发了一次,消息真的送了。"""
    from app.db import get_db
    sent = []
    monkeypatch.setattr(client.app.state, "deliver_outreach",
                        lambda d: sent.append(d["id"]) or True)
    r = client.post(f"/api/admin/growth/drafts/{coupon_draft}/approve", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["sent"] is True
    assert sent == [coupon_draft]
    grants = get_db().list_user_grants("u1")
    assert len(grants) == 1 and grants[0]["code"] == "SHOE30"
    assert get_db().get_outreach_draft(coupon_draft)["status"] == "sent"


def test_approve_retry_after_delivery_failure_does_not_dead_end(
        client, coupon_draft, monkeypatch):
    """这正是本次要修的 Critical:发券成功→投递失败→退回待审→店主重试。

    修复前:第二次批准时 issue_for_draft 对同一条草稿重新调用 grant_coupon,
    撞上 UNIQUE(code, user_id) 约束返回 None,被误判成"重复发放"而拒绝——
    这条草稿从此再也送不出去,买家却已经真的拿到了那张券。
    修复后:issue_for_draft 认出这行发放记录是这同一条草稿发的,放行继续
    投递,重试第二次才真正送达。全程券只发了一次。
    """
    from app.db import get_db
    calls = {"n": 0}

    def _deliver(d):
        calls["n"] += 1
        return calls["n"] > 1   # 第一次投递失败,第二次(重试)成功

    monkeypatch.setattr(client.app.state, "deliver_outreach", _deliver)

    r1 = client.post(f"/api/admin/growth/drafts/{coupon_draft}/approve", headers=AUTH)
    body1 = r1.json()
    assert body1["success"] is False and body1["sent"] is False
    assert "投递失败" in body1["reason"] and "可重试" in body1["reason"]
    assert get_db().get_outreach_draft(coupon_draft)["status"] == "draft"   # 退回待审
    assert len(get_db().list_user_grants("u1")) == 1   # 钱已经花过一次,没被撤销

    r2 = client.post(f"/api/admin/growth/drafts/{coupon_draft}/approve", headers=AUTH)
    body2 = r2.json()
    assert body2["success"] is True and body2["sent"] is True

    assert calls["n"] == 2   # 投递被真正重试了一次,不是第一次失败后就没再调用
    grants = get_db().list_user_grants("u1")
    assert len(grants) == 1   # 重试没有让券被发第二次
    assert get_db().get_outreach_draft(coupon_draft)["status"] == "sent"


def test_approve_with_unknown_coupon_code_is_refused(
        client, unknown_coupon_draft, monkeypatch):
    """模型编的券码:拒绝发放,不投递,且这条拒绝是永久性的——不能说可重试。"""
    from app.db import get_db
    sent = []
    monkeypatch.setattr(client.app.state, "deliver_outreach",
                        lambda d: sent.append(1) or True)
    r = client.post(f"/api/admin/growth/drafts/{unknown_coupon_draft}/approve", headers=AUTH)
    body = r.json()
    assert body["success"] is False and body["sent"] is False
    assert "券码" in body["reason"]
    assert "可重试" not in body["reason"]   # 永久性拒绝,重试结果不会变
    assert sent == []                       # 没投递
    assert get_db().list_user_grants("u1") == []   # 没发放
    assert get_db().get_outreach_draft(unknown_coupon_draft)["status"] == "draft"


def test_approve_cross_draft_duplicate_coupon_is_refused(
        client, coupon_draft, monkeypatch):
    """这张券已经被**另一条草稿**发给了这个买家:真正的重复,必须拒绝——
    "同一草稿可重试"的放行不能连带把跨草稿的重复也一起放行了。"""
    from app.db import get_db
    get_db().grant_coupon("SHOE30", "u1", 999, "另一条草稿发的", "admin")

    sent = []
    monkeypatch.setattr(client.app.state, "deliver_outreach",
                        lambda d: sent.append(1) or True)
    r = client.post(f"/api/admin/growth/drafts/{coupon_draft}/approve", headers=AUTH)
    body = r.json()
    assert body["success"] is False and body["sent"] is False
    assert "重复发放" in body["reason"]
    assert "可重试" not in body["reason"]   # 同样是永久性拒绝
    assert sent == []
    assert len(get_db().list_user_grants("u1")) == 1   # 仍是原来那一条,没有新增
    assert get_db().get_outreach_draft(coupon_draft)["status"] == "draft"
