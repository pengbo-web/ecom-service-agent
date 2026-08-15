"""失败归因分层:只让**可修的**信号回流到 skill 自改进。

论文明确警告:没有归因筛选就驱动修订,会把不可修的信号错误编码成知识。本仓库有
这个形态最干净的实证——44 条失败轨迹里 **41 条是归属校验正确地拦住了跨用户访问**
(订单 ORD-20240115-001 属于「小明」,打出失败的是 ab0/ev3/trk5 这类压测用户),
3 条是会话过短,**真正的知识缺口 0 条**。改造前这 44 条会被整批喂给 improve_skill,
产出的每一份"改进候选"都建立在非信号上。

本文件守两条:
1. 每一类都判得对,**尤其是归属校验那条**——它在错误文案层面与"订单真的不存在"
   完全不可区分(话术刻意做成这样以免泄露订单存在性);
2. 判不出时**说判不出**,不默认塞进任何一类。塞进 knowledge_gap 就是"不知道就
   当成要改",正是论文警告的那件事;塞进 evaluation_noise 会把真实缺陷悄悄丢掉。
"""

import json

import pytest

from app.agent.skills.attribution import (
    CATEGORY_CAPABILITY_LIMIT,
    CATEGORY_EVALUATION_NOISE,
    CATEGORY_KNOWLEDGE_GAP,
    CATEGORY_UNDETERMINED,
    METHOD_MODEL,
    METHOD_NONE,
    METHOD_RULE,
    attribute,
    partition,
    summarize,
)


class FakeDB:
    """只实现 `get_order`。归因唯一用到的库操作就是它。"""

    def __init__(self, orders=None):
        self.orders = orders or {}
        self.calls = 0

    def get_order(self, order_id):
        self.calls += 1
        return self.orders.get(order_id)


class ExplodingClient:
    """被调用就炸。用来钉死"确定性规则命中时绝不问模型"。"""

    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                raise AssertionError("规则已经能判定,不该再调模型")


def _trace(user_id="buyer-1", skill="track-order", outcome="tool_error", calls=None):
    return {"session_id": "s1", "user_id": user_id, "skill_name": skill,
            "outcome": outcome, "tool_calls": calls or []}


def _call(name="query_order", ok=False, error="未找到订单 ORD-1，请核实订单号",
          args=None, **extra):
    return {"name": name, "ok": ok, "error": error, "args": args or {}, **extra}


def _archive(session_id="s1", turns=2, human_reply=""):
    msgs = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"第{i}句买家消息,内容够长了"})
        msgs.append({"role": "assistant", "content": "好的"})
    if human_reply:
        msgs.append({"role": "assistant",
                     "content": json.dumps({"intent": "human_agent",
                                            "reply": human_reply}, ensure_ascii=False)})
    return {"session_id": session_id, "messages": msgs}


@pytest.fixture
def auth_on(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    return settings


# --------------------------------------------------------------------------
# ② 归属校验 —— 本项目最大的一类误判来源
# --------------------------------------------------------------------------

def test_cross_user_order_access_is_a_capability_limit_not_a_knowledge_gap(auth_on):
    """**这条是整个阶段二存在的理由。**

    错误文案是「未找到订单 ORD-1」——与"订单真的不存在"逐字相同(话术刻意做成
    这样,不泄露订单存在性)。在字符串层面永远分不开,只有查库才知道:
    这单存在,但不是这个人的。
    """
    db = FakeDB({"ORD-1": {"order_id": "ORD-1", "user": "小明"}})
    r = attribute(_trace(user_id="trk5", calls=[_call(args={"order_id": "ORD-1"})]),
                  _archive(), db=db)
    assert r["category"] == CATEGORY_CAPABILITY_LIMIT
    assert r["rule"] == "ownership_denied"
    assert r["flows_back"] is False
    assert "小明" not in r["evidence"][0]      # 证据里不该把别人的名字带出来
    assert "trk5" in r["evidence"][0]


def test_order_owned_by_the_asker_is_not_an_ownership_denial(auth_on):
    """自己的订单查失败是**真失败**,不能被这条规则吞掉。"""
    db = FakeDB({"ORD-1": {"order_id": "ORD-1", "user": "buyer-1"}})
    r = attribute(_trace(user_id="buyer-1", calls=[_call(args={"order_id": "ORD-1"})]),
                  _archive(), db=db)
    assert r["rule"] != "ownership_denied"


def test_order_that_truly_does_not_exist_is_not_an_ownership_denial(auth_on):
    """库里根本没有这单 —— 那就是字面意思,不是权限边界。"""
    r = attribute(_trace(calls=[_call(args={"order_id": "ORD-404"})]),
                  _archive(), db=FakeDB())
    assert r["rule"] != "ownership_denied"


def test_auth_disabled_means_the_ownership_rule_does_not_apply(monkeypatch):
    """`auth_enabled=False` 时归属校验根本不生效,"未找到订单"就是字面意思。
    照旧判成权限边界会把一个真实的数据问题永久藏起来。"""
    from app.config.settings import settings

    monkeypatch.setattr(settings, "auth_enabled", False)
    db = FakeDB({"ORD-1": {"order_id": "ORD-1", "user": "小明"}})
    r = attribute(_trace(user_id="trk5", calls=[_call(args={"order_id": "ORD-1"})]),
                  _archive(), db=db)
    assert r["rule"] != "ownership_denied"


def test_missing_user_id_does_not_guess(auth_on):
    """轨迹没记 user_id 就判不出归属,不能拿"没有 user_id"当成"不是他的"。"""
    db = FakeDB({"ORD-1": {"order_id": "ORD-1", "user": "小明"}})
    r = attribute(_trace(user_id="", calls=[_call(args={"order_id": "ORD-1"})]),
                  _archive(), db=db)
    assert r["rule"] != "ownership_denied"


# --------------------------------------------------------------------------
# ① 基础设施 / ③ 授权门 / ④ 守卫 / ⑤ 会话过短
# --------------------------------------------------------------------------

def test_infrastructure_failures_are_capability_limits(auth_on):
    r = attribute(_trace(calls=[_call(error="ConnectError: 无法连接上游服务")]),
                  _archive(), db=FakeDB())
    assert (r["category"], r["rule"]) == (CATEGORY_CAPABILITY_LIMIT, "infra_only")


def test_infra_rule_runs_before_ownership(auth_on):
    """顺序本身是判断的一部分:一次超时里带的订单号说明不了任何事。"""
    db = FakeDB({"ORD-1": {"order_id": "ORD-1", "user": "小明"}})
    r = attribute(_trace(user_id="trk5",
                         calls=[_call(error="ReadTimeout", args={"order_id": "ORD-1"})]),
                  _archive(), db=db)
    assert r["rule"] == "infra_only"


def test_consent_gate_is_a_capability_limit(auth_on):
    """前置授权门未放行 = 用户还没确认,工具按设计不执行。

    判据是轨迹里的 `need_confirm` **结构化标记**,不是匹配那句确认话术——
    措辞一改,字符串判据就静默失效,而失效的表现是"确认门拦下的动作被当成
    知识缺口去补",没有任何报错。
    """
    r = attribute(_trace(calls=[_call(name="apply_refund", error="请确认是否办理退款",
                                      need_confirm=True)]),
                  _archive(), db=FakeDB())
    assert (r["category"], r["rule"]) == (CATEGORY_CAPABILITY_LIMIT, "consent_required")


def test_guard_blocked_only_is_evaluation_noise(auth_on):
    """守卫拦住跳步、模型随后补齐,是守卫**起作用**而非本轮失败。"""
    r = attribute(_trace(calls=[{"name": "apply_refund", "ok": False,
                                 "error": "必须先 query_order", "args": {},
                                 "blocked": True}]),
                  _archive(), db=FakeDB())
    assert (r["category"], r["rule"]) == (CATEGORY_EVALUATION_NOISE, "guard_blocked_only")


def test_short_session_is_evaluation_noise(auth_on):
    """买家没把诉求说完就走了,不构成"skill 没搞定"的证据。"""
    r = attribute(_trace(calls=[_call()]), _archive(turns=1), db=FakeDB())
    assert (r["category"], r["rule"]) == (CATEGORY_EVALUATION_NOISE, "session_too_short")


# --------------------------------------------------------------------------
# ⑥ 知识缺口 —— 唯一回流的一类
# --------------------------------------------------------------------------

def test_human_takeover_is_the_knowledge_gap_signal(auth_on):
    """人工接管并回了话 = 有 human reference 可对照。论文全部反馈信号的来源
    就是这种工单。"""
    r = attribute(_trace(calls=[_call()]),
                  _archive(human_reply="超过7天的订单按平台规则不能退,但可以走质保"),
                  db=FakeDB())
    assert r["category"] == CATEGORY_KNOWLEDGE_GAP
    assert r["flows_back"] is True


def test_only_knowledge_gap_flows_back(auth_on):
    """回流判定不能各处自己写一遍。"""
    db = FakeDB({"ORD-1": {"order_id": "ORD-1", "user": "小明"}})
    for trace, archive in (
        (_trace(user_id="trk5", calls=[_call(args={"order_id": "ORD-1"})]), _archive()),
        (_trace(calls=[_call(error="ReadTimeout")]), _archive()),
        (_trace(calls=[_call()]), _archive(turns=1)),
        (_trace(calls=[_call()]), _archive()),
    ):
        assert attribute(trace, archive, db=db)["flows_back"] is False


# --------------------------------------------------------------------------
# 确定性优先于模型
# --------------------------------------------------------------------------

@pytest.mark.parametrize("trace,archive", [
    (_trace(calls=[_call(error="ConnectError 无法连接")]), _archive()),
    (_trace(calls=[_call(name="apply_refund", error="请确认", need_confirm=True)]),
     _archive()),
    (_trace(calls=[_call()]), _archive(turns=1)),
])
def test_deterministic_rules_never_call_the_model(auth_on, trace, archive):
    """**方案里明写的验收项。** 能用确定性规则判的绝不问模型:每一次调用都是钱、
    都是延迟、都是一次可能猜错的机会。传一个"被调用就炸"的 client 来钉死它。"""
    r = attribute(trace, archive, db=FakeDB(), client=ExplodingClient(), model="m")
    assert r["method"] == METHOD_RULE


def test_ownership_rule_does_not_call_the_model(auth_on):
    db = FakeDB({"ORD-1": {"order_id": "ORD-1", "user": "小明"}})
    r = attribute(_trace(user_id="trk5", calls=[_call(args={"order_id": "ORD-1"})]),
                  _archive(), db=db, client=ExplodingClient(), model="m")
    assert r["method"] == METHOD_RULE


def test_model_only_refines_the_human_takeover_branch(auth_on):
    """人工确实回了,但**回的是知识还是让步**——只有这一处值得花一次调用。

    典型的假知识缺口:「这次给您破例全额退,下不为例」。那是授权范围内的一次
    让步,照抄进 SKILL.md 就把一次破例变成了一条政策,而 SKILL.md 是要被当作
    指令执行的。
    """
    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    class M:
                        content = "capability_limit"
                    return type("R", (), {"choices": [type("C", (), {"message": M})]})

    r = attribute(_trace(calls=[_call()]),
                  _archive(human_reply="这次给您破例全额退,下不为例"),
                  db=FakeDB(), client=Client(), model="m")
    assert (r["category"], r["method"]) == (CATEGORY_CAPABILITY_LIMIT, METHOD_MODEL)


def test_model_failure_falls_back_to_the_rule_verdict(auth_on):
    """归因失败**绝不能编一个类别出来**。模型炸了就退回规则判定,并如实
    把 method 标成 rule。"""
    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("端点挂了")

    r = attribute(_trace(calls=[_call()]),
                  _archive(human_reply="超过7天按平台规则不能退"),
                  db=FakeDB(), client=Client(), model="m")
    assert (r["category"], r["method"]) == (CATEGORY_KNOWLEDGE_GAP, METHOD_RULE)


def test_unparseable_model_answer_falls_back_too(auth_on):
    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    class M:
                        content = "嗯……这个不好说"
                    return type("R", (), {"choices": [type("C", (), {"message": M})]})

    r = attribute(_trace(calls=[_call()]), _archive(human_reply="按平台规则处理"),
                  db=FakeDB(), client=Client(), model="m")
    assert r["method"] == METHOD_RULE


# --------------------------------------------------------------------------
# 判不出就说判不出
# --------------------------------------------------------------------------

def test_unmatched_failure_is_undetermined_not_silently_bucketed(auth_on):
    """**第四类存在的全部理由。**

    塞进 knowledge_gap = "不知道就当成要改",正是论文警告的那件事;
    塞进 evaluation_noise = 把真实缺陷悄悄丢掉。所以它单独成一类:
    不回流,但如实报数——这个数字大起来说明判据不够用了。
    """
    r = attribute(_trace(calls=[_call(error="订单状态异常,处理失败")]),
                  _archive(), db=FakeDB())
    assert (r["category"], r["method"]) == (CATEGORY_UNDETERMINED, METHOD_NONE)
    assert r["flows_back"] is False


def test_db_read_failure_does_not_invent_a_verdict(auth_on):
    """离线归因读库失败 = 判不出,不能因为读不到就当成"不是他的"。"""
    class BrokenDB:
        def get_order(self, oid):
            raise RuntimeError("库挂了")

    r = attribute(_trace(user_id="trk5", calls=[_call(args={"order_id": "ORD-1"})]),
                  _archive(), db=BrokenDB())
    assert r["rule"] != "ownership_denied"


# --------------------------------------------------------------------------
# 批量与汇报
# --------------------------------------------------------------------------

def test_partition_buckets_and_only_returns_knowledge_gaps_for_reflow(auth_on):
    db = FakeDB({"ORD-1": {"order_id": "ORD-1", "user": "小明"}})
    traces = [
        _trace(user_id="trk5", calls=[_call(args={"order_id": "ORD-1"})]),
        {**_trace(calls=[_call()]), "session_id": "s2"},
    ]
    archives = [_archive("s1"), _archive("s2", human_reply="按平台7天规则处理")]
    r = partition(traces, archives, db=db)
    assert r["counts"][CATEGORY_CAPABILITY_LIMIT] == 1
    assert r["counts"][CATEGORY_KNOWLEDGE_GAP] == 1
    assert len(r["flows_back"]) == 1


def test_summary_reports_every_category_including_undetermined(auth_on):
    """**每一类都要报数,包括判不出的。** 只报"回流了几条"会让被丢掉的那些
    永远不进人的视野,而它们才是需要有人去看的。"""
    r = partition([_trace(calls=[_call(error="订单状态异常")])], [_archive()],
                  db=FakeDB())
    line = summarize(r)
    for word in ("知识缺口", "能力/权限边界", "评测噪声", "判不出"):
        assert word in line


def test_attribution_on_the_real_corpus_finds_the_ownership_wall():
    """**对真实库跑一遍。** 这条是本轮改造的实证:44 条失败里 41 条是归属校验
    正确拦截,真知识缺口 0 条 —— 改造前它们会被整批喂给 improve_skill。

    数字会随库变化,所以只断言"绝大多数不是知识缺口",不钉死具体值。
    """
    from app.db import get_db

    db = get_db()
    traces = db.list_skill_traces(outcomes=["tool_error", "handoff"], limit=1000)
    if not traces:
        pytest.skip("本机库里没有失败轨迹")
    r = partition(traces, db.list_recent_archives(limit=300), db=db)
    total = sum(r["counts"].values())
    assert r["counts"][CATEGORY_KNOWLEDGE_GAP] < total * 0.5, (
        f"真实语料里过半失败被判成知识缺口,判据可能失效了: {r['counts']}")
