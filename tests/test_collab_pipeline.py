"""协作链路:信号→诊断→草稿的串联、correlation 贯通、LLM 失败降级、不给投诉用户推销。"""

import json
from unittest.mock import patch

import pytest

from app.db.database import Database
from app.multi_agent import bus, collab, shared_context as sc


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """隔离数据库。

    用 `set_db()` 换全局单例,而不是逐个模块 monkeypatch `get_db`:各模块都是
    在调用时才 `get_db()`(读 app.db._DB 这个全局),换单例一处就全覆盖。逐个
    patch 的写法还有个隐患——总线改走 EventBus 载体之后 `bus` 模块不再 import
    `get_db`,那种 fixture 会直接 AttributeError,而它跟被测行为毫无关系。
    """
    from app.db import set_db
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    set_db(d)
    yield d
    set_db(None)


def _unpaid(d, oid, user, sku="P001"):
    """造一条"已付款但久拖不发"的订单,供 find_opportunities("stale_pending_order") 命中。

    命名沿用历史(改名会牵动本文件十几处调用),但含义要说准:status='pending'
    在本项目里是**已付款待发货**。真正的"未支付"是 N5 之后新增的
    status='unpaid',见下面的 `_really_unpaid`——这个函数名与语义的错位,正是
    当年那句"该项目订单表没有未付款状态"的注释留下的痕迹,那句话现在已经不成立。

    判定"久拖不发"靠 created_at 早于 48 小时前;这里设成 3 天前,落在窗口
    (14 天)内、又晚于 48 小时阈值。
    """
    conn = d.connect()
    try:
        conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                     "VALUES (?,?,'pending',199,datetime('now','-3 days'))", (oid, user))
        conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                     "VALUES (?, '跑鞋',?,1,199)", (oid, sku))
        conn.commit()
    finally:
        conn.close()


def _really_unpaid(d, oid, user, sku="P001"):
    """造一条**真·未支付**订单(status='unpaid',N5 起的真实状态),
    供 find_opportunities("unpaid_order") 命中。下单时间设 3 天前,
    远超 settings.unpaid_stale_hours(默认 24h)。"""
    conn = d.connect()
    try:
        conn.execute("INSERT INTO orders (order_id,user,status,total,created_at) "
                     "VALUES (?,?,'unpaid',199,datetime('now','-3 days'))", (oid, user))
        conn.execute("INSERT INTO order_items (order_id,name,sku,quantity,price) "
                     "VALUES (?, '跑鞋',?,1,199)", (oid, sku))
        conn.commit()
    finally:
        conn.close()


def _stale_cart(d, user, sku="P001"):
    """造一条久未转化的购物车行,供 find_opportunities("abandoned_cart") 命中
    (默认 cart_stale_hours=48,这里放 3 天前)。"""
    conn = d.connect()
    try:
        conn.execute("INSERT INTO carts (user_id,sku,quantity,status,added_at) "
                     "VALUES (?,?,1,'active',datetime('now','-3 days'))", (user, sku))
        conn.commit()
    finally:
        conn.close()


def _signal(d, corr="C1", kind="refund_rate_high", subject="P001"):
    return d.publish_event(bus.EV_SIGNAL_ANOMALY, {
        "kind": kind, "subject": subject, "subject_name": "跑鞋",
        "value": 0.3, "threshold": 0.15,
        "detail": {"orders": 20, "refunds": 6, "top_reason": "尺码不准"},
    }, bus.AGENT_SERVICE, bus.AGENT_ANALYST, corr)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse("已生成")


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()


def test_draft_prompt_states_real_situation_not_bare_kind(monkeypatch):
    """回归测试:_llm_draft 拼给模型的 prompt 必须包含中文情境说明与订单真实
    状态,而不能让模型只看到一个裸的英文 kind 去自己猜——这正是"待支付"
    误写事故的根因。同时验证数据段改用 JSON 序列化(而不是 Python repr),
    否则中文会被转成 \\uXXXX 转义,模型读到的就不是真人能看懂的中文。"""
    fake_client = _FakeClient()
    monkeypatch.setattr(collab, "_collab_client", lambda: fake_client)

    opportunity = {
        "kind": "stale_pending_order",
        "situation_label": "下单后久未推进",
        "order_id": "O1", "user_id": "u1",
        "order_status": "pending", "order_status_label": "待发货",
        "amount": 199.0, "created_at": "2026-01-01 00:00:00", "items": "跑鞋",
    }
    diagnosis = {"conclusion": "尺码问题"}

    collab._llm_draft(diagnosis, opportunity)

    assert len(fake_client.chat.completions.calls) == 1
    prompt = fake_client.chat.completions.calls[0]["messages"][0]["content"]

    # 情境必须落在 prompt 里,写手不用再靠猜的
    assert "下单后久未推进" in prompt
    assert "待发货" in prompt
    # 数据段确实是 JSON(ensure_ascii=False),而不是 f"{opportunity}" 的 repr——
    # repr 里的中文会被转义成 \uXXXX,不会原样出现在 prompt 文本里。
    assert json.dumps(opportunity, ensure_ascii=False) in prompt
    # 裸的英文 kind 值不能是对这个情境的唯一描述——它可以仍然出现在 JSON
    # 数据里,但必须同时伴有中文情境说明,不能是模型唯一能看到的线索。
    assert "situation_label" in prompt or "下单后久未推进" in prompt


def test_analyst_writes_diagnosis_and_forwards(db):
    _signal(db)
    with patch.object(collab, "_llm_explain", return_value="尺码标注不符,建议更新尺码表"):
        stats = collab.run_once()
    assert stats["analyst"]["done"] == 1
    entry = sc.fetch_entry(sc.KEY_DIAGNOSIS, "P001")
    assert entry["source_agent"] == bus.AGENT_ANALYST
    assert "尺码" in entry["value"]["conclusion"]
    forwarded = [e for e in db.list_events() if e["event_type"] == bus.EV_INSIGHT_DIAGNOSIS]
    assert len(forwarded) == 1
    assert forwarded[0]["target_agent"] == bus.AGENT_GROWTH


def test_correlation_id_runs_through_whole_chain(db):
    """run_once() 一次调用就打通 signal→diagnosis→drafts 全链路(见 run_once 文档字符串),
    不需要连续调两次去"分段推进"。"""
    _unpaid(db, "O1", "u1")
    _signal(db, corr="CHAIN")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="这款鞋已更新尺码建议,可以参考下"):
        collab.run_once()
    chain = db.list_events(correlation_id="CHAIN")
    kinds = {e["event_type"] for e in chain}
    assert bus.EV_SIGNAL_ANOMALY in kinds
    assert bus.EV_INSIGHT_DIAGNOSIS in kinds
    assert bus.EV_DRAFTS_READY in kinds
    assert db.list_outreach_drafts(status="draft")[0]["correlation_id"] == "CHAIN"


def test_llm_failure_degrades_but_chain_continues(db):
    """归因用的 LLM 挂了,链路要继续:写一条"只有事实、没有归因"的诊断。"""
    _signal(db)
    with patch.object(collab, "_llm_explain", side_effect=RuntimeError("llm down")):
        stats = collab.run_once()
    assert stats["analyst"]["failed"] == 0        # 不算处理失败
    entry = sc.fetch(sc.KEY_DIAGNOSIS, "P001")
    assert entry["degraded"] is True
    assert entry["conclusion"]                    # 仍有可读文本(纯统计事实)


def test_escalation_signal_does_not_trigger_marketing(db):
    """刚转过人工的会话不该被拿去推销。单次 run_once() 即可验证:handle_signal
    从不为这类信号发布 insight.diagnosis,growth 段自然没有任何事件可消费。"""
    _unpaid(db, "O1", "u1")
    db.publish_event(bus.EV_SIGNAL_ANOMALY,
                     {"kind": "service_escalation", "subject": "s1", "session_id": "s1"},
                     bus.AGENT_SERVICE, bus.AGENT_ANALYST, "C9")
    with patch.object(collab, "_llm_explain", return_value="x"):
        collab.run_once()
    assert db.list_outreach_drafts() == []


def test_growth_creates_one_draft_per_opportunity(db):
    """单次 run_once() 已足以走完 signal→diagnosis→drafts 全链路。"""
    _unpaid(db, "O1", "u1")
    _unpaid(db, "O2", "u2")
    _signal(db, corr="C2")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="尺码建议已更新"):
        collab.run_once()
    assert len(db.list_outreach_drafts(status="draft")) == 2


def test_run_once_is_idempotent(db):
    """这里连续调两次是有意为之,验证的是"第二次没有剩余事件可消费"
    (claimed == 0),不是"每次调用推进一个阶段"——第一次调用已经把
    全链路跑完了(见 run_once 文档字符串)。"""
    _signal(db)
    with patch.object(collab, "_llm_explain", return_value="x"):
        collab.run_once()
        second = collab.run_once()
    assert second["analyst"]["claimed"] == 0


def test_disabled_switch_is_a_no_op(db, monkeypatch):
    from app.config import settings as st
    _signal(db)
    monkeypatch.setattr(st.settings, "collab_enabled", False)
    stats = collab.run_once()
    assert stats["analyst"]["claimed"] == 0
    assert stats["reclaimed"] == 0   # 开关关掉时回收也必须静默,不能悄悄改库


def test_worker_cycle_reclaims_stranded_events(db):
    """【I1】worker 崩在 claim 与 finish 之间留下的事件,必须能经 **worker 路径**
    自己回到队列。

    `reclaim_stale_events` 一直存在,但在这次修复前**只有测试调它**:
    `run_once()` 没调、CLI 的 cycle() 没调、启动路径也没调。于是
    `bus._finish_and_count` 的 docstring 里那句"交给 reclaim_stale_events 或人工
    决定"实际上只剩"人工"——被认领后遇上进程被杀的事件永久停在 processing。

    这条测试刻意**不直接调 db.reclaim_stale_events**(那样只会重新验证数据层,
    正是修复前就已经绿着的那部分),而是模拟崩溃后走 `collab.run_once()`,
    断言事件真的被本轮重新认领并处理掉。
    """
    _signal(db)
    # 模拟"认领后 worker 就挂了":claim 把行置成 processing,finish 永远没发生
    claimed = db.claim_events(bus.AGENT_ANALYST)
    assert len(claimed) == 1
    stranded_id = claimed[0]["id"]
    conn = db.connect()
    try:   # 把 consumed_at 拨老,越过回收阈值
        conn.execute("UPDATE agent_events SET consumed_at = datetime('now','-1 hours') "
                     "WHERE id = ?", (stranded_id,))
        conn.commit()
    finally:
        conn.close()
    assert [e for e in db.list_events() if e["id"] == stranded_id][0]["status"] == "processing"

    with patch.object(collab, "_llm_explain", return_value="尺码问题"):
        stats = collab.run_once()

    assert stats["reclaimed"] == 1
    assert stats["analyst"]["claimed"] == 1, "回收必须发生在消费之前,本轮就该被重新认领"
    assert [e for e in db.list_events() if e["id"] == stranded_id][0]["status"] == "done"


def test_fresh_processing_event_is_not_stolen_by_reclaim(db):
    """回收不得把**刚刚**被认领、还在正常处理中的事件抢回来(否则会重复处理)。"""
    _signal(db)
    claimed = db.claim_events(bus.AGENT_ANALYST)
    assert len(claimed) == 1
    stats = collab.run_once()          # consumed_at 是刚才,远没到 300 秒阈值
    assert stats["reclaimed"] == 0
    assert stats["analyst"]["claimed"] == 0


def test_two_insights_on_one_chain_do_not_double_queue_the_same_buyer(db):
    """【I3】一次扫描判定多个 SKU 异常 → 多条 insight.diagnosis → 同一批商机被
    重复起草,同一个买家在审批队列里躺着好几条几乎相同的草稿。

    人工闸挡得住"自动发出",挡不住审批队列被灌满——而店主是挨个批下去的,
    批完就等于给同一个买家连发了好几条。所以起草侧要跳过"该买家/该订单已经
    有一条待审草稿"的商机。
    """
    _unpaid(db, "O1", "u1")
    _signal(db, corr="C9", subject="P001")
    _signal(db, corr="C9", subject="P002")   # 同一条链上的第二个跨线商品

    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="尺码建议已更新"):
        collab.run_once()

    drafts = db.list_outreach_drafts(status="draft")
    assert len(drafts) == 1, f"同一买家同一订单被排了 {len(drafts)} 次队"
    assert drafts[0]["user_id"] == "u1" and drafts[0]["order_id"] == "O1"


def test_dedupe_key_is_per_order_not_per_buyer(db):
    """去重键是 (user_id, order_id):同一个买家名下**两笔不同**的滞留订单是两件
    真事,合并掉会让其中一笔永远得不到触达。"""
    _unpaid(db, "O1", "u1")
    _unpaid(db, "O2", "u1")   # 同一个买家,另一笔订单
    _signal(db, corr="C10")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="话术"):
        collab.run_once()
    drafts = db.list_outreach_drafts(status="draft")
    assert {d["order_id"] for d in drafts} == {"O1", "O2"}


def test_dedupe_ignores_already_reviewed_drafts(db):
    """只有**还在待审**的草稿参与去重:店主处理过的历史不该永久封杀再次触达。"""
    _unpaid(db, "O1", "u1")
    did = db.create_outreach_draft("stale_pending_order", "u1", "O1", "旧的", {},
                                   "r", "C0", "growth")
    db.review_outreach_draft(did, "rejected", "admin")   # 已驳回 → 不再挡新草稿
    _signal(db, corr="C11")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="新话术"):
        collab.run_once()
    assert len(db.list_outreach_drafts(status="draft")) == 1


def test_degraded_diagnosis_is_not_forwarded_to_marketing(db):
    """归因不可用的降级诊断不该被拿去写营销话术:_llm_draft 会把 conclusion
    原文嵌进买家消息,"仅列事实、归因暂不可用"这种半成品文案没有意义,
    重新扫一次的成本又很低——所以 handle_signal 的转发闸门里加了
    `not degraded` 这一条,这里把它钉死成回归测试。"""
    _unpaid(db, "O1", "u1")
    _signal(db, corr="C5", kind="refund_rate_high")
    with patch.object(collab, "_llm_explain", side_effect=RuntimeError("llm down")):
        stats = collab.run_once()
    assert stats["analyst"]["done"] == 1
    forwarded = [e for e in db.list_events() if e["event_type"] == bus.EV_INSIGHT_DIAGNOSIS]
    assert forwarded == []
    assert db.list_outreach_drafts() == []


def test_attach_correlation_failure_does_not_lose_drafts_or_fail_event(db, monkeypatch):
    """_attach_correlation 挂链失败(比如 UPDATE 抛异常)只是丢一个可追溯性标签,
    不该把已经成功创建的草稿"弄丢",也不该让整条 handle_insight 事件被判 failed
    (failed 的事件不会自动重试,会永久卡住)。"""
    _unpaid(db, "O1", "u1")
    _unpaid(db, "O2", "u2")
    _signal(db, corr="C3")

    real_get_db = collab.get_db
    calls = {"n": 0}

    class _FlakyDB:
        def connect(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated db failure on first attach")
            return real_get_db().connect()

    monkeypatch.setattr(collab, "get_db", lambda: _FlakyDB())

    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="尺码建议已更新"):
        stats = collab.run_once()

    assert stats["growth"]["failed"] == 0
    drafts = db.list_outreach_drafts(status="draft")
    assert len(drafts) == 2  # 两条草稿都还在,没有因为挂链失败而丢失
    corr_ids = {d["correlation_id"] for d in drafts}
    assert "C3" in corr_ids            # 第二条挂链成功
    assert any(c != "C3" for c in corr_ids)  # 第一条挂链失败,保留了 draft_outreach 自己生成的 corr


# ---------- 阶段一 gap②:协作链两次 LLM 调用必须可观测,且与 correlation_id 绑定 ----------

def test_collab_client_routes_through_langfuse_wrapper(monkeypatch):
    """_collab_client() 必须经 make_openai_client 包装,而不是裸 OpenAI(...)——
    否则门控开着也没用,因为构造点本身就绕开了 drop-in。"""
    calls = {}

    def _fake_make_client(**kwargs):
        calls.update(kwargs)
        return "sentinel-client"

    monkeypatch.setattr("app.observability.langfuse_client.make_openai_client",
                        _fake_make_client)
    client = collab._collab_client()
    assert client == "sentinel-client"
    assert "timeout" in calls and "max_retries" in calls


class _FakeTraceRoot:
    def __init__(self):
        self.updates = []

    def update(self, **kw):
        self.updates.append(kw)


def _fake_background_trace(calls):
    """造一个假的 `background_trace`:记录每次开的 trace 名/session_id,
    yield 一个能收 .update() 的假根观察。"""
    from contextlib import contextmanager

    @contextmanager
    def _bt(name, session_id=None, user_id=None, input=None):
        root = _FakeTraceRoot()
        calls.append({"name": name, "session_id": session_id, "root": root})
        yield root

    return _bt


def test_handle_signal_wraps_attribution_with_correlation_as_session(db, monkeypatch):
    """归因这一步必须包进一条以 correlation_id 为 Langfuse session_id 的命名
    trace——参谋这一段与营销那一段(handle_insight)共享同一个 corr,
    Langfuse 会话视图才能把两段协作步骤分到同一条链上。"""
    calls: list = []
    monkeypatch.setattr("app.observability.langfuse_bridge.background_trace",
                        _fake_background_trace(calls))
    _signal(db, corr="TRACE1")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"):
        collab.run_once()
    # 该信号是 MARKETING_WORTHY(refund_rate_high),归因段之后 growth 段也会
    # 开自己的一条 trace(即便没有商机可起草)——这里只断言归因段那一条存在。
    attribution_calls = [c for c in calls if c["name"] == "collab_analyst_attribution"]
    assert len(attribution_calls) == 1
    assert attribution_calls[0]["session_id"] == "TRACE1"
    assert attribution_calls[0]["root"].updates   # 结论写回了 trace 的 output


def test_handle_insight_wraps_drafting_with_correlation_as_session(db, monkeypatch):
    """起草这一步同样包进以 correlation_id 为 session_id 的命名 trace,
    与归因那条共享同一个 corr,构成信号→归因→起草的完整链路视图。"""
    calls: list = []
    monkeypatch.setattr("app.observability.langfuse_bridge.background_trace",
                        _fake_background_trace(calls))
    _unpaid(db, "O1", "u1")
    _signal(db, corr="TRACE2")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="话术"):
        collab.run_once()
    names = {c["name"] for c in calls}
    sids = {c["session_id"] for c in calls}
    assert names == {"collab_analyst_attribution", "collab_growth_drafting"}
    assert sids == {"TRACE2"}   # 两段共享同一个 correlation_id 作 session_id


def test_collab_degrades_silently_when_langfuse_init_raises(db, monkeypatch):
    """核心 fail-soft 性质:门控开着,但 Langfuse SDK 初始化本身抛异常——协作
    链路的判定结果(归因是否降级、是否转发、草稿条数)必须与完全不接
    Langfuse 时逐字节一致,不能被观测层的异常打断或改变。"""
    import app.observability.langfuse_bridge as bridge_mod
    from app.config import settings as st
    monkeypatch.setattr(st.settings, "langfuse_enabled", True)
    monkeypatch.setattr(bridge_mod, "_ensure_env",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    _unpaid(db, "O1", "u1")
    _signal(db, corr="TRACE3")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="话术"):
        stats = collab.run_once()
    assert stats["analyst"]["done"] == 1
    assert stats["growth"]["done"] == 1
    assert len(db.list_outreach_drafts(status="draft")) == 1


def test_collab_unaffected_when_langfuse_disabled(db):
    """门控关(默认态):协作链路行为不变,这是回归测试的基线对照。"""
    _unpaid(db, "O1", "u1")
    _signal(db, corr="TRACE4")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="话术"):
        stats = collab.run_once()
    assert stats["analyst"]["done"] == 1
    assert stats["growth"]["done"] == 1
    assert len(db.list_outreach_drafts(status="draft")) == 1


def test_attach_correlation_does_not_rewrite_reviewed_draft(db):
    """人工已经审批过的草稿,correlation_id 不该被再改写——状态守卫只对
    仍是 draft(待审)的行生效。"""
    _unpaid(db, "O1", "u1")
    _signal(db, corr="C4")
    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="尺码建议已更新"):
        collab.run_once()

    draft = db.list_outreach_drafts(status="draft")[0]
    assert db.review_outreach_draft(draft["id"], "approved", reviewed_by="owner1")

    changed = collab._attach_correlation(draft["id"], "SHOULD-NOT-APPLY")

    assert changed is False
    reviewed = db.get_outreach_draft(draft["id"])
    assert reviewed["status"] == "approved"
    assert reviewed["correlation_id"] != "SHOULD-NOT-APPLY"


# ---------- 自主链的商机覆盖面(修 3 回归) ----------

def test_autonomous_drafting_covers_unpaid_and_abandoned_cart(db):
    """自主协作链必须能对**催付款/弃单挽回**起草,不只是"已付款久拖"。

    这里曾经是硬编码 kind="stale_pending_order":全自动那条链只可能产出一种
    草稿,而"加购未付款、大量未支付订单"——交付文案的头号场景——自主链路
    一辈子碰不到,只能靠店主在对话里手动点名。N5 早就给了它们真实数据
    (orders.status='unpaid'、carts 表),缺的只是这里放行。
    """
    _really_unpaid(db, "O-UNPAID", "u_unpaid")
    _stale_cart(db, "u_cart")
    _unpaid(db, "O-STALE", "u_stale")          # 已付款久拖(旧口径)
    _signal(db, corr="CK")

    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="给您留意了一下这笔单"):
        collab.run_once()

    drafts = db.list_outreach_drafts(status="draft", limit=50)
    kinds = {d["opportunity_type"] for d in drafts}
    assert "unpaid_order" in kinds, "自主链没有为未支付订单起草(kind 又被写死了?)"
    assert "abandoned_cart" in kinds, "自主链没有为弃单起草"
    assert "stale_pending_order" in kinds, "原有的久拖商机不能因为扩面而丢掉"


def test_autonomous_draft_records_each_opportunity_own_kind(db):
    """每条草稿的 opportunity_type 必须是**这条商机自己的** kind。

    写错的代价是实打实的:跟进链按 opportunity_type 判"商机还开着没"
    (followup._opportunity_still_open)。把一条未支付商机记成
    stale_pending_order,买家付了款(unpaid→pending)链也停不下来,
    会继续给一个已经付过钱的人发催付款提醒。
    """
    _really_unpaid(db, "O-UNPAID", "u_unpaid")
    _signal(db, corr="CK2")

    with patch.object(collab, "_llm_explain", return_value="尺码问题"), \
         patch.object(collab, "_llm_draft", return_value="您这单还差一步付款"):
        collab.run_once()

    rows = [d for d in db.list_outreach_drafts(status="draft", limit=50)
            if d["user_id"] == "u_unpaid"]
    assert rows, "未支付买家没有拿到草稿"
    assert all(r["opportunity_type"] == "unpaid_order" for r in rows), \
        f"kind 记错了: {[r['opportunity_type'] for r in rows]}"
