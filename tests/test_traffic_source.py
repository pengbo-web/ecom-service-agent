"""流量来源标记:别拿压测数据决定线上技能的生死。

**为什么加这个字段。** 实测 `skill_traces` 976 轮的构成:

    u1 等测试固定用户 / 人工走查   858 轮
    hmdp 真实买家                  62 轮
    压测(ab*)                      41 轮
    评测沙箱(ev*/trk*/nat*)        15 轮

而在这个字段落地之前,表里**没有任何东西能把它们分开**。`watchdog.evaluate_absolute`
拿这张表算成功率,`rate < 0.6 → ROLLBACK` —— 也就是说**一轮压测就能把一份没问题的
skill 从线上换掉**。

标注之后 `track-order` 的实测差别:全量 53 轮 28%,**只算真实流量 17 轮 88%**。
那 28% 压根不是关于这份 skill 的陈述。

本文件守四件事:
1. 默认是 `live`,而合成流量的每个产生点显式标注自己;
2. 请求头**只能往"非真实"方向标**,不能自称 live;
3. 判定类(看门狗)只认 live,采样类(语料)认 live+unknown 但排除压测/评测/模拟;
4. 历史行是 `unknown`,不是 `live` —— 判不出就写判不出。
"""

import pytest

from app.agent.runtime_context import (
    DECISION_SOURCES,
    SAMPLING_SOURCES,
    SOURCE_DEV,
    SOURCE_EVAL,
    SOURCE_LIVE,
    SOURCE_LOADTEST,
    SOURCE_SIMULATED,
    SOURCE_UNKNOWN,
    get_traffic_source,
    set_traffic_source,
)


@pytest.fixture(autouse=True)
def _reset():
    set_traffic_source(None)
    yield
    set_traffic_source(None)


# --------------------------------------------------------------------------
# 默认值与取值合法性
# --------------------------------------------------------------------------

def test_default_is_live():
    """**默认必须是 live。**

    真实请求走的是默认路径,而合成流量的每一个产生点都是我们自己写的代码,
    由它显式标注。反过来把默认设成 unknown,会让真实流量因为某处忘了标注而被
    判定层整批丢掉 —— 那时看门狗永远样本不足,自动收口彻底停摆。
    """
    assert get_traffic_source() == SOURCE_LIVE


def test_bogus_value_falls_back_to_unknown_not_live():
    """拼错的字符串不能静默变成一个新"来源" —— 那会让"只认 live"的判定层
    看起来一切正常,实际把某一类流量整批漏算了。

    落到 unknown 而不是 live:判不出时**不假装是真实流量**。
    """
    set_traffic_source("loadtestt")
    assert get_traffic_source() == SOURCE_UNKNOWN


def test_none_resets_to_default():
    set_traffic_source(SOURCE_LOADTEST)
    set_traffic_source(None)
    assert get_traffic_source() == SOURCE_LIVE


# --------------------------------------------------------------------------
# 两套口径的差别是刻意的
# --------------------------------------------------------------------------

def test_decision_scope_is_live_only():
    """判定错 = 把一份没问题的 skill 从线上回滚掉,**立刻生效且不可白做**。
    所以它连 unknown 都不认。"""
    assert DECISION_SOURCES == {SOURCE_LIVE}


def test_sampling_scope_keeps_unknown_but_drops_synthetic():
    """采样错 = 多学了一段不该学的对话,后面还有校验/门禁/风险分级/人工审批
    四道关。所以容得下 unknown(否则既有 87 条归档语料全部作废,阶段一白做)。

    但压测/评测/模拟两边都排除:拿自己生成的对话当"真实买家语料"蒸馏进
    SKILL.md,是在学自己的回声。`simulated` 尤其危险 —— 阶段三的多轮模拟本身
    就是拿 skill 生成的,再喂回去合成门禁用例就成了闭环自证。
    """
    assert SOURCE_UNKNOWN in SAMPLING_SOURCES
    assert SOURCE_LIVE in SAMPLING_SOURCES
    for synthetic in (SOURCE_LOADTEST, SOURCE_EVAL, SOURCE_SIMULATED):
        assert synthetic not in SAMPLING_SOURCES
        assert synthetic not in DECISION_SOURCES


def test_dev_traffic_is_excluded_from_both():
    """人工走查造的账号(u1/measure_user/framecheck…)实测占 858 轮 —— 它比
    压测还多。既不能算成绩,也不该当语料。"""
    assert SOURCE_DEV not in DECISION_SOURCES
    assert SOURCE_DEV not in SAMPLING_SOURCES


# --------------------------------------------------------------------------
# 请求头只能降级
# --------------------------------------------------------------------------

@pytest.mark.parametrize("header,expected", [
    ("loadtest", SOURCE_LOADTEST),
    ("eval", SOURCE_EVAL),
    ("dev", SOURCE_DEV),
    ("LOADTEST", SOURCE_LOADTEST),          # 大小写不敏感
    ("  loadtest  ", SOURCE_LOADTEST),      # 前后空白
])
def test_header_can_mark_traffic_as_synthetic(header, expected):
    from app.api.app import _mark_traffic_source

    _mark_traffic_source(header)
    assert get_traffic_source() == expected


@pytest.mark.parametrize("header", ["live", "LIVE", "不认识的值", "", None])
def test_header_can_never_claim_traffic_is_real(header):
    """**这条是安全边界。**

    默认已经是 live,所以这个头唯一的合法用途是让脚本给自己降级。若允许请求方把
    任意流量标成 live,这个字段就成了一个**可以被伪造的"真实性证明"**——
    而看门狗恰恰拿它做自动回滚判定。
    """
    from app.api.app import _mark_traffic_source

    _mark_traffic_source(header)
    assert get_traffic_source() == SOURCE_LIVE


# --------------------------------------------------------------------------
# 落库与过滤
# --------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    from app.db import Database

    d = Database(str(tmp_path / "t.db"))
    d.init_schema()
    return d


def test_trace_records_the_current_source(db):
    set_traffic_source(SOURCE_LOADTEST)
    db.record_skill_trace("s1", "ab0", "track-order", [], "success")
    assert db.skill_trace_source_counts() == {SOURCE_LOADTEST: 1}


def test_explicit_source_argument_wins(db):
    """离线脚本没有 contextvar 可依赖,得能直接传。"""
    db.record_skill_trace("s1", "u", "track-order", [], "success",
                          source=SOURCE_SIMULATED)
    assert db.skill_trace_source_counts() == {SOURCE_SIMULATED: 1}


def test_filters_separate_real_traffic_from_the_rest(db):
    set_traffic_source(SOURCE_LIVE)
    for i in range(9):
        db.record_skill_trace(f"L{i}", "1", "track-order", [], "success")
    set_traffic_source(SOURCE_LOADTEST)
    for i in range(20):
        db.record_skill_trace(f"S{i}", f"ab{i}", "track-order",
                              [{"name": "query_order", "ok": False, "error": "未找到订单"}],
                              "tool_error")

    all_counts = db.skill_trace_counts()["track-order"]
    live_counts = db.skill_trace_counts(sources=[SOURCE_LIVE])["track-order"]
    assert all_counts == {"success": 9, "tool_error": 20}
    assert live_counts == {"success": 9}
    assert len(db.list_skill_traces(sources=[SOURCE_LIVE], limit=100)) == 9


def test_archive_carries_source_too(db):
    """归档是**门禁用例合成与失败改进的语料源**。一段压测对话被当成真实买家
    语料蒸馏进 SKILL.md,和一条压测轨迹被算进成功率一样糟——只是后者立刻显形,
    前者要等到线上说错话才显形。"""
    set_traffic_source(SOURCE_LOADTEST)
    db.archive_session("s1", "ab0", [{"role": "user", "content": "压测语料"}], None)
    set_traffic_source(SOURCE_LIVE)
    db.archive_session("s2", "1", [{"role": "user", "content": "真实提问"}], None)

    assert len(db.list_recent_archives(limit=10)) == 2
    live = db.list_recent_archives(limit=10, sources=[SOURCE_LIVE])
    assert len(live) == 1 and live[0]["session_id"] == "s2"


def test_null_source_rows_are_counted_as_unknown_not_dropped(db):
    """老库补列时历史行已回填成 'unknown',但别的写入路径仍可能留下 NULL。
    **NULL 不等于任何值**,会被 IN 静默漏掉 —— 那些行既不算 live 也不算 unknown,
    凭空从统计里消失。COALESCE 兜住它。"""
    db.record_skill_trace("s1", "u", "track-order", [], "success")
    conn = db.connect()
    conn.execute("UPDATE skill_traces SET source = NULL")
    conn.commit()
    conn.close()

    assert db.skill_trace_source_counts() == {SOURCE_UNKNOWN: 1}
    assert db.skill_trace_counts(sources=[SOURCE_UNKNOWN])["track-order"] == {"success": 1}


def test_migration_marks_historical_rows_unknown_not_live(db):
    """**迁移里唯一要紧的那个决定。**

    字段加上之前,表里确实没有任何东西能区分真实与合成。把历史行补成 live 等于
    凭空断言"这些都是真实流量",而看门狗会拿这个断言去做自动回滚。
    """
    conn = db.connect()
    conn.execute("INSERT INTO skill_traces (session_id, user_id, skill_name, "
                 "tool_calls, outcome, created_at) VALUES ('s','u','x','[]','success','t')")
    conn.commit()
    conn.close()
    assert db.skill_trace_source_counts().get(SOURCE_UNKNOWN) == 1


# --------------------------------------------------------------------------
# 这才是它真正防住的事
# --------------------------------------------------------------------------

def test_stress_traffic_would_otherwise_roll_back_a_healthy_skill():
    """**这个字段存在的全部理由,用一个构造的例子钉死。**

    一份真实表现 90% 的 skill,叠加一轮同等规模的压测(打的是别人的订单号,
    归属校验按设计拒绝)→ 混合成功率 0.50。那正落在 `AB_SANITY_FLOOR=0.3` 与
    `ABSOLUTE_MIN_RATE=0.6` 之间 —— 可信下限挡不住的那一段,会真的自动回滚。
    """
    from app.agent.skills import risk as R
    from app.agent.skills.watchdog import (DECISION_PROMOTE, DECISION_ROLLBACK,
                                           evaluate_absolute)

    def rows(n, ok, source):
        return [{"outcome": "success" if i < ok else "tool_error",
                 "variant": "live", "source": source} for i in range(n)]

    real = rows(40, 36, SOURCE_LIVE)          # 真实 90%
    stress = rows(40, 4, SOURCE_LOADTEST)     # 压测 10%

    mixed = evaluate_absolute(real + stress, min_samples=R.ABSOLUTE_MIN_SAMPLES,
                              min_rate=R.ABSOLUTE_MIN_RATE)
    only_live = evaluate_absolute(real, min_samples=R.ABSOLUTE_MIN_SAMPLES,
                                  min_rate=R.ABSOLUTE_MIN_RATE)

    assert mixed["decision"] == DECISION_ROLLBACK, "前提变了:混合数据不再触发回滚"
    assert only_live["decision"] == DECISION_PROMOTE
    # 0.5 必须真的落在"挡不住"的那一段,否则这条测试证明不了什么
    assert R.AB_SANITY_FLOOR < 0.5 < R.ABSOLUTE_MIN_RATE


def test_watchdog_queries_live_only():
    """看门狗那一处调用必须显式带 sources —— 漏了就等于这个字段没做。"""
    import inspect

    from app.scripts import skill_watchdog

    src = inspect.getsource(skill_watchdog.check_canaries)
    assert "sources=list(DECISION_SOURCES)" in src, (
        "check_canaries 没有按 live 过滤轨迹,压测流量会参与自动回滚判定")


# --------------------------------------------------------------------------
# 回填:启发式,先看后写
# --------------------------------------------------------------------------

@pytest.mark.parametrize("user_id,expected", [
    ("ab0", SOURCE_LOADTEST),
    ("lcd-3", SOURCE_LOADTEST),
    ("ev5", SOURCE_EVAL),
    ("trk2", SOURCE_EVAL),
    ("u1", SOURCE_DEV),
    ("measure_user", SOURCE_DEV),
    ("1011", SOURCE_LIVE),
    ("alice", SOURCE_UNKNOWN),      # 读不出理由 → 不猜
    ("", SOURCE_UNKNOWN),
    (None, SOURCE_UNKNOWN),
])
def test_backfill_rules(user_id, expected):
    from app.scripts.backfill_traffic_source import classify

    assert classify(user_id)[0] == expected


def test_backfill_states_a_reason_for_every_rule():
    """每一条规则都要能说出"凭什么"。读不出理由的形态不该有规则 ——
    那就是在编。"""
    from app.scripts.backfill_traffic_source import RULES

    for _pattern, source, why in RULES:
        assert why.strip(), f"{source} 这条规则没写依据"


def test_backfill_preview_does_not_write(db, monkeypatch):
    """默认只打印方案。启发式不该藏在一次静默的写库里 —— 它得有人看过、认过。"""
    from app.scripts import backfill_traffic_source as bf

    set_traffic_source(None)
    conn = db.connect()
    conn.execute("INSERT INTO skill_traces (session_id, user_id, skill_name, "
                 "tool_calls, outcome, created_at) VALUES ('s','ab0','x','[]','success','t')")
    conn.commit()
    conn.close()

    monkeypatch.setattr("app.db.get_db", lambda: db)
    bf.main([])                                     # 不带 --apply
    assert db.skill_trace_source_counts() == {SOURCE_UNKNOWN: 1}

    bf.main(["--apply"])
    assert db.skill_trace_source_counts() == {SOURCE_LOADTEST: 1}


# --------------------------------------------------------------------------
# 跨执行边界:这一处静默失效过一次
# --------------------------------------------------------------------------

def test_streaming_takes_the_source_as_an_argument_not_from_contextvar():
    """**实测:靠 contextvar 传过去是不работает的,而且完全静默。**

    `/api/chat` 返回 `StreamingResponse` + **同步**生成器,Starlette 用
    `iterate_in_threadpool` 驱动它 —— 每次 `next()` 都在一份新拷贝的上下文里跑,
    生成器体内 `yield` 之前设的 contextvar,下一次恢复时已经没了。

    第一版就是在端点里设 contextvar,自测五种取值**全部落成 live**:
    `X-Traffic-Source` 这个头等于不存在,而它本该是压测流量唯一的标记手段。
    改成显式参数,由真正写轨迹的 worker 线程自己设。

    这条测试钉的是"参数还在、worker 里还设"——它是这个功能唯一的传递路径。
    """
    import inspect

    from app.api import streaming

    sig = inspect.signature(streaming.run_agent_streaming)
    assert "traffic_source" in sig.parameters, (
        "run_agent_streaming 少了 traffic_source 参数 —— 压测流量会重新落成 live")

    src = inspect.getsource(streaming.run_agent_streaming)
    assert "set_traffic_source(traffic_source)" in src, (
        "worker 线程里没有设来源,参数传了也没用")


def test_chat_endpoint_passes_the_source_through():
    """端点必须把它传下去。少这一行,上面那条也白搭。"""
    import inspect

    from app.api import app as app_mod

    src = inspect.getsource(app_mod.create_app)
    assert "traffic_source=_traffic_src" in src, (
        "/api/chat 没有把解析出来的来源传给 run_agent_streaming")
