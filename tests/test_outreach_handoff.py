"""触达承接:营销投一条消息进买家会话之后,下一轮怎么被正确接住。

修的缺陷:投递走 `_append_agent_reply(intent="growth_outreach")`,把消息追加进买家
会话,然后营销退场。那条消息常以问句结尾(实测 draft 41:「需要帮您看看是卡在哪儿
了吗?」),买家回一句"好啊",接手的画像**看得到那句话,却不知道它是店铺主动发的、
带了哪张券、关联哪一单**。

**刻意没有新建"营销承接画像"。** presale 手上早就有 query_coupons / query_product /
place_order / add_to_cart——回答营销问题、推动付款本来就是它的活;新建一个只读画像
等于做一个功能更弱的 presale 副本。缺的从头到尾只是把上下文带过去。
"""

import json
from datetime import datetime, timedelta

import pytest

from app.config.settings import settings
from app.db.database import Database
from app.multi_agent import outreach_context as oc


def _ts(**kw):
    return (datetime.now() + timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


def _envelope(reply: str, intent: str = "human_agent") -> dict:
    """与 `_append_agent_reply` 逐字段同构的信封。"""
    return {"role": "assistant", "content": json.dumps(
        {"intent": intent, "confidence": 1.0, "reply": reply,
         "requires_human": False, "follow_up_question": None},
        ensure_ascii=False)}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    from app.db import set_db
    set_db(d)
    monkeypatch.setattr(settings, "outreach_context_window_hours", 24.0)
    yield d
    set_db(None)


def _sent(d: Database, *, kind="unpaid_order", user="u1", order="O1",
          coupon="SHOE30", sent_at=None, reason="内部经营判断,不该外泄") -> int:
    did = d.create_outreach_draft(
        kind, user, order, "您的订单还差一步就完成啦",
        {"coupon_code": coupon} if coupon else {}, reason, "C1", "growth")
    conn = d.connect()
    try:
        conn.execute("UPDATE outreach_drafts SET status='sent', sent_at=? WHERE id=?",
                     (sent_at or _ts(minutes=-5), did))
        conn.commit()
    finally:
        conn.close()
    return did


# ------------------------------------------------------------------ 反查

def test_finds_a_recent_sent_outreach(db):
    _sent(db)
    o = oc.recent_outreach("u1")
    assert o["opportunity_type"] == "unpaid_order"
    assert o["order_id"] == "O1"
    assert o["offer"] == {"coupon_code": "SHOE30"}      # JSON 已反序列化


def test_only_sent_outreach_is_handed_off(db):
    """待审草稿买家**根本没收到**,拿它做前情会让客服提起一件没发生的事。"""
    db.create_outreach_draft("unpaid_order", "u1", "O1", "还没发", {}, "", "C1", "growth")
    assert oc.recent_outreach("u1") is None


def test_outreach_outside_the_window_is_not_handed_off(db):
    """过了承接窗口,买家这一轮大概率是新问题。"""
    _sent(db, sent_at=_ts(hours=-30))
    assert oc.recent_outreach("u1") is None


def test_window_zero_disables_handoff(db, monkeypatch):
    monkeypatch.setattr(settings, "outreach_context_window_hours", 0)
    _sent(db)
    assert oc.recent_outreach("u1") is None


def test_lookup_is_per_buyer(db):
    _sent(db, user="u1")
    assert oc.recent_outreach("u2") is None


def test_lookup_failure_is_fail_soft(db, monkeypatch):
    """这条读在买家会话热路径上:拿不到就当没有触达,不能让这一轮失败。"""
    def boom(*_a, **_k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "recent_sent_outreach", boom)
    assert oc.recent_outreach("u1", db=db) is None


def test_takes_the_latest_when_several_were_sent(db):
    _sent(db, order="OLD", sent_at=_ts(hours=-10))
    _sent(db, order="NEW", sent_at=_ts(minutes=-2))
    assert oc.recent_outreach("u1")["order_id"] == "NEW"


# ------------------------------------------------------- awaiting_reply

def test_awaiting_reply_when_outreach_is_the_last_assistant_message():
    msgs = [{"role": "user", "content": "在吗"},
            _envelope("在的"),
            _envelope("您的订单还差一步", intent="growth_outreach")]
    assert oc.awaiting_reply(msgs) is True


def test_not_awaiting_once_the_buyer_has_replied():
    """**这是 awaiting_reply 的全部意义。** 承接窗口是 24 小时,买家可能在这期间已经
    聊了三轮别的事;那时触达偏好不该再压过粘性路由,否则会被硬拽回催付款。"""
    msgs = [_envelope("您的订单还差一步", intent="growth_outreach"),
            {"role": "user", "content": "好啊帮我看看"},
            _envelope("好的,我看到这单还没付款"),
            {"role": "user", "content": "算了,我想退掉另一单"}]
    assert oc.awaiting_reply(msgs) is False


def test_ordinary_assistant_reply_is_not_an_outreach():
    """判据取信封里的 intent,不是"最后一条是不是 assistant"——坐席人工回复、
    正常客服回答都是 assistant。"""
    msgs = [_envelope("您的订单还差一步", intent="growth_outreach"),
            {"role": "user", "content": "好啊"},
            _envelope("已为您查询到订单状态")]
    assert oc.awaiting_reply(msgs) is False


def test_plain_text_assistant_message_is_not_an_outreach():
    assert oc.awaiting_reply([{"role": "assistant", "content": "纯文本回复"}]) is False


def test_tool_messages_between_do_not_break_detection():
    msgs = [_envelope("您的订单还差一步", intent="growth_outreach"),
            {"role": "tool", "content": "{}"}]
    assert oc.awaiting_reply(msgs) is True


def test_awaiting_reply_on_empty_history():
    assert oc.awaiting_reply([]) is False
    assert oc.awaiting_reply(None) is False


# -------------------------------------------------------- 路由承接偏好

@pytest.mark.parametrize("kind,expected", [
    ("unpaid_order", "presale"),        # 想付款 → presale 有 place_order/query_coupons
    ("abandoned_cart", "presale"),
    ("stalled_bargain", "presale"),
    ("consulted_no_order", "presale"),
    ("stale_pending_order", "midsale"),  # 想知道货到哪了 → midsale 有 query_logistics
    ("shipped_no_care", "midsale"),
    ("delivered_no_review", "aftersale"),
])
def test_preferred_profile_by_opportunity_kind(kind, expected):
    assert oc.preferred_profile({"opportunity_type": kind}) == expected


def test_unknown_kind_falls_back_to_the_existing_default():
    """未登记返回 None,由调用方回落既有 `DEFAULT_AGENT`(= aftersale)。

    **绝不能让营销偏好把这个兜底改掉**:履约诉求被误路由到一个没有 `apply_refund`
    的画像是真实伤害,反过来只是答得笼统一点——两者不对称。
    """
    assert oc.preferred_profile({"opportunity_type": "brand_new_kind"}) is None
    assert oc.preferred_profile(None) is None


def test_preferred_profile_targets_only_real_profiles():
    """映射表里的每个值都必须是真实存在的买家侧画像。写错一个名字的后果是
    `profiles.get(key)` 落到 `next(iter(...))` —— 字典序第一个画像,与意图毫无
    关系,而且不报错。"""
    from app.multi_agent.agents import AGENT_CONFIGS
    assert set(oc._PREFERRED_PROFILE.values()) <= set(AGENT_CONFIGS)


def test_every_opportunity_kind_has_a_preferred_profile():
    """`growth.OPPORTUNITY_KINDS` 加一类商机时,这里必须同步表态——否则那类触达的
    承接会静默回落到 aftersale,而症状只是"路由有点怪"。"""
    from app.agent.tools.growth import OPPORTUNITY_KINDS
    assert set(OPPORTUNITY_KINDS) == set(oc._PREFERRED_PROFILE)


def test_router_hint_carries_the_situation_only():
    """路由器只需判断意图归属:给它券码/订单号只会让那个 10-token 的分类跑偏。"""
    hint = oc.router_hint({"opportunity_type": "unpaid_order", "order_id": "O1",
                           "offer": {"coupon_code": "SHOE30"}})
    assert "下单未支付" in hint
    assert "O1" not in hint and "SHOE30" not in hint


def test_router_hint_is_empty_without_outreach():
    assert oc.router_hint(None) == ""
    assert oc.router_hint({"opportunity_type": "brand_new_kind"}) == ""


# --------------------------------------------------------- prompt 注入

def test_render_includes_situation_order_and_coupon():
    txt = oc.render_outreach_context(
        {"opportunity_type": "unpaid_order", "order_id": "ORD-9",
         "offer": {"coupon_code": "SHOE30"}})
    assert "下单未支付" in txt
    assert "ORD-9" in txt
    assert "SHOE30" in txt
    assert "不要让顾客重复说明来意" in txt


def test_render_never_leaks_the_approval_reason():
    """`reason` 是给**店主**看的经营判断。进了买家上下文,客服就可能说出"我们这款鞋
    退款率确实偏高"——把内部经营数据以"客服亲口承认"的形式泄露出去。

    这与 `buyer_hints` 的设计前提是同一条:走确定性映射,让泄露"结构上不可能",
    而不是靠 prompt 里加一句"不要透露"(那次品牌语气实验已经证明 prompt 禁令
    保得住动作、保不住话术)。
    """
    txt = oc.render_outreach_context(
        {"opportunity_type": "unpaid_order", "order_id": "O1", "offer": {},
         "reason": "该款跑鞋尺码偏大,导致 6 笔退款,建议在详情页添加尺码提醒",
         "needs_review_reason": "内部审核备注", "correlation_id": "SCAN-abc"})
    for leaked in ("尺码偏大", "6 笔退款", "详情页", "内部审核备注", "SCAN-abc"):
        assert leaked not in txt


def test_render_forbids_answering_coupon_rules_from_memory():
    """券码本身已经发给买家了(投递前 issue_for_draft 已发放),告诉画像不构成泄露。
    但**规则必须查**——凭印象说明门槛的后果是顾客照着一个编出来的门槛去下单。"""
    txt = oc.render_outreach_context(
        {"opportunity_type": "unpaid_order", "order_id": "", "offer":
            {"coupon_code": "SHOE30"}})
    assert "query_coupons" in txt


def test_render_omits_the_order_line_when_there_is_no_order():
    """弃单/咨询未下单的商机 order_id 恒为空串。"""
    txt = oc.render_outreach_context(
        {"opportunity_type": "abandoned_cart", "order_id": "", "offer": {}})
    assert "加购未下单" in txt
    assert "关联订单" not in txt


def test_render_is_empty_for_unknown_situations():
    """认不出情境时返回空,而不是给一段泛化的"顾客刚收到过一条消息"——那会让客服
    的开场变成在追问一件它自己也不知道是什么的事。"""
    assert oc.render_outreach_context({"opportunity_type": "brand_new_kind"}) == ""
    assert oc.render_outreach_context(None) == ""


def test_kind_label_reuses_the_growth_table():
    """标签不在这里另抄一份:抄一份会漂移(那边改了措辞这里还是旧的),而症状是
    "买家侧提示里的情境说得不对",没有任何报错。"""
    from app.agent.tools.growth import OPPORTUNITY_KINDS
    txt = oc.render_outreach_context(
        {"opportunity_type": "stale_pending_order", "order_id": "", "offer": {}})
    assert OPPORTUNITY_KINDS["stale_pending_order"] in txt


# ------------------------------------------------- 编排器接线(不调 LLM)

def test_orchestrator_injects_the_block_into_the_system_prompt(db, monkeypatch):
    from app.multi_agent.orchestrator import MultiAgentOrchestrator
    monkeypatch.setattr(settings, "mcp_enabled", False)
    orch = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)   # 不建引擎
    orch._outreach = {"opportunity_type": "unpaid_order", "order_id": "ORD-9",
                      "offer": {"coupon_code": "SHOE30"}}
    block = orch._outreach_context_block()
    assert "下单未支付" in block and "ORD-9" in block


def test_orchestrator_block_is_empty_without_outreach():
    from app.multi_agent.orchestrator import MultiAgentOrchestrator
    orch = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)
    orch._outreach = None
    assert orch._outreach_context_block() == ""


def test_orchestrator_preferred_requires_awaiting_reply():
    from app.multi_agent.orchestrator import MultiAgentOrchestrator
    orch = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)
    orch.profiles = {"presale": {}, "midsale": {}, "aftersale": {}}
    orch._outreach = {"opportunity_type": "unpaid_order"}

    orch._outreach_awaiting = True
    assert orch._outreach_preferred() == "presale"
    orch._outreach_awaiting = False
    assert orch._outreach_preferred() is None      # 话题已走开


def test_orchestrator_rejects_a_profile_that_does_not_exist():
    """映射表指向一个不存在的画像时必须返回 None,不能让它流到
    `profiles.get(key) or next(iter(...))` —— 那会静默落到字典序第一个画像。"""
    from app.multi_agent.orchestrator import MultiAgentOrchestrator
    orch = MultiAgentOrchestrator.__new__(MultiAgentOrchestrator)
    orch.profiles = {"aftersale": {}}              # presale 不存在
    orch._outreach = {"opportunity_type": "unpaid_order"}
    orch._outreach_awaiting = True
    assert orch._outreach_preferred() is None


def test_router_accepts_and_prepends_the_hint():
    """前情拼在最前:它是这一轮的前提,不是补充说明。"""
    from app.multi_agent.router import Router

    captured = {}

    class FakeClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    captured["prompt"] = kw["messages"][0]["content"]

                    class R:
                        choices = [type("C", (), {"message": type(
                            "M", (), {"content": "presale"})()})()]
                    return R()

    key = Router(FakeClient(), "m").route(
        "好啊帮我看看", history=None,
        outreach_hint="注意:店铺刚刚主动给这位顾客发过一条消息,情境是「下单未支付」。")
    assert key == "presale"
    assert captured["prompt"].startswith("注意:店铺刚刚主动")


def test_understand_receives_the_hint_and_puts_it_first():
    """**这是实测 bug 的回归钉子。**

    改造第一版只把前情接到了 `Router.route` 上,而生产主路径是
    `understanding.understand()`(`query_understanding_enabled` 默认开)。它只看最近
    5 条 **user** 消息,营销触达是 assistant 消息——对它完全不可见。

    实测后果:一条催付款触达之后买家回「好啊,帮我看看」,这一步判成
    `intent=订单事务 → aftersale`;而 aftersale 没有 `query_coupons`、没有
    `place_order`,既讲不清随触达发出的那张券,也帮不了买家把这单付掉。
    接上前情之后同一句话判成 `商品咨询 → presale`(线上实测两次对照)。

    而"编排层做回落偏好"这个方案**修不了它**:回落只在这一步判不出域时生效,
    而它几乎总能判出一个域,只是判错了。
    """
    from app.agent import understanding

    captured = {}

    class FakeClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    captured["prompt"] = kw["messages"][0]["content"]

                    class R:
                        choices = [type("C", (), {"message": type("M", (), {
                            "content": '{"domain":"presale","intent":"商品咨询",'
                                       '"need_kb":false,"kb_query":null}'})()})()]
                    return R()

    hint = "注意:店铺刚刚主动给这位顾客发过一条消息,情境是「下单未支付」。"
    understanding.understand("帮我看看这单怎么还没走完流程呢", [], FakeClient(), "m",
                             outreach_hint=hint)
    prompt = captured["prompt"]
    assert hint in prompt
    # 落在"最近对话"块的**开头**:它是这一轮的前提,要排在历史各条之前。
    # 用 rindex 找那个字段标签——「用户最新消息」这个词在开头的指令句里也出现,
    # 用 index 会命中指令而不是字段(这条断言第一版就是这么写错的)。
    assert prompt.index("最近对话") < prompt.index(hint) < prompt.rindex("用户最新消息:")


def test_understand_without_hint_is_unchanged():
    from app.agent import understanding

    prompts = []

    class FakeClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    prompts.append(kw["messages"][0]["content"])

                    class R:
                        choices = [type("C", (), {"message": type("M", (), {
                            "content": '{"domain":"aftersale","intent":"退款",'
                                       '"need_kb":false,"kb_query":null}'})()})()]
                    return R()

    c = FakeClient()
    long_enough = "我这单到底什么时候能发货啊已经等了好几天了"
    understanding.understand(long_enough, [], c, "m")
    understanding.understand(long_enough, [], c, "m", outreach_hint="")
    assert prompts[0] == prompts[1]


def test_router_without_hint_is_byte_identical_to_before():
    from app.multi_agent.router import Router

    prompts = []

    class FakeClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    prompts.append(kw["messages"][0]["content"])

                    class R:
                        choices = [type("C", (), {"message": type(
                            "M", (), {"content": "aftersale"})()})()]
                    return R()

    c = FakeClient()
    Router(c, "m").route("我要退款")
    Router(c, "m").route("我要退款", outreach_hint="")
    assert prompts[0] == prompts[1]
