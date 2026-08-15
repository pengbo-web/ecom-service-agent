"""门禁用例自动合成:解死结,但**不能靠放松标准来解**。

死结(实测,`--start-all` 每一轮都走同一条):新建 skill 无对照组 → 走
gate_then_watch → 要求过离线门禁 → 门禁要求评测集里有用例点名它 → 没有任何机制
为新 skill 产用例 → 永远 `gate_unavailable`。

自动造用例最容易造出来的东西是**看起来通过了的假门禁**。本文件守的就是这个:
合成出来的每一条断言都必须能追溯到真实发生过的事,合成用例必须与人工用例分开
报数,且绝不能污染回归基线。

对应 `docs/SkillEvo借鉴技术方案.md` 阶段一的三条纪律与验收项。
"""

import json

import pytest

from app.agent.skills.case_synthesis import (
    SOURCE_KEYWORD,
    SOURCE_TRACE,
    SYNTH_ID_PREFIX,
    Turn,
    case_from_turn,
    is_synthetic_case_id,
    load_synth_cases,
    save_synth_cases,
    skill_actor,
    skill_keywords,
    split_turns,
    synth_path,
    synthesize_gate_cases,
    tool_universe,
)

KNOWN = {"query_order", "query_logistics", "list_user_orders", "apply_refund"}


def _msg(role, content="", tools=None):
    m = {"role": role, "content": content}
    if tools:
        m["tool_calls"] = [{"type": "function", "function": {"name": t, "arguments": "{}"}}
                           for t in tools]
    return m


def _archive(session_id, messages, user_id="u1"):
    return {"session_id": session_id, "user_id": user_id, "messages": messages}


def _trace(session_id, skill_name, variant="live"):
    return {"session_id": session_id, "skill_name": skill_name, "variant": variant,
            "outcome": "success", "tool_calls": []}


# --------------------------------------------------------------------------
# 切轮次:断言的原材料从哪来
# --------------------------------------------------------------------------

def test_split_turns_attributes_tools_to_the_asking_turn():
    """工具要归到**发问的那一轮**,而不是整段会话铺平。

    铺平会让第 8 轮的退款工具变成第 1 轮"你好"的期望。
    """
    turns = split_turns([
        _msg("user", "帮我查下 ORD-1 的物流"),
        _msg("assistant", "好的", tools=["query_order"]),
        _msg("tool", '{"success": true}'),
        _msg("assistant", "已发货"),
        _msg("user", "那我要退货"),
        _msg("assistant", "", tools=["apply_refund"]),
    ])
    assert [t.user for t in turns] == ["帮我查下 ORD-1 的物流", "那我要退货"]
    assert turns[0].tools == ["query_order"]
    assert turns[1].tools == ["apply_refund"]


def test_split_turns_accepts_unparsed_json_string():
    """`Database.get_archived_session` 返回的 messages 是**没有反序列化的 JSON 串**,
    而 `list_recent_archives` 反序列化了。两个调用方形状不同,这里要都吃得下。"""
    raw = json.dumps([_msg("user", "查订单"), _msg("assistant", "好", tools=["query_order"])])
    turns = split_turns(raw)
    assert len(turns) == 1 and turns[0].tools == ["query_order"]


@pytest.mark.parametrize("bad", [None, "", "not json", 42, {"a": 1}])
def test_split_turns_never_raises_on_garbage(bad):
    """单条脏数据不该崩掉整批合成。"""
    assert split_turns(bad) == []


def test_split_turns_ignores_messages_before_first_user():
    """首条 user 之前的系统消息不属于任何一轮,不能挂到不存在的轮上。"""
    assert split_turns([_msg("assistant", "您好", tools=["query_order"])]) == []


# --------------------------------------------------------------------------
# 断言只能来自事实
# --------------------------------------------------------------------------

def test_expected_tools_come_from_the_real_call_and_nothing_else():
    case = case_from_turn("track-order", Turn(user="帮我查下 ORD-1 的物流",
                                              tools=["query_order", "query_logistics"]),
                          known_tools=KNOWN)
    assert case["expected_tools"] == ["query_order", "query_logistics"]
    assert case["turns"] == ["帮我查下 ORD-1 的物流"]     # 买家原话逐字,不改写
    assert case["related_skills"] == ["track-order"]


def test_tools_missing_from_the_registry_are_dropped():
    """轨迹可能有几个月历史,期间工具被改名/下线。

    断言一个已经不存在的工具会让这条用例**永远无法通过**,而门禁 fail-closed
    ——这个 skill 就被一条过期用例永久钉死了。这比没有用例更糟:没有用例至少
    会打 `gate_unavailable` 提示人来看,永远失败看起来像"候选质量不行"。
    """
    case = case_from_turn("track-order",
                          Turn(user="帮我查下 ORD-1 的物流",
                               tools=["query_order", "order_list_v1_deprecated"]),
                          known_tools=KNOWN)
    assert case["expected_tools"] == ["query_order"]


def test_load_skill_is_not_an_expected_tool():
    """`load_skill` 是内部机制。服务端确定性预加载开启时它根本不会被模型调用,
    断言它等于要求"模型必须自己想到去加载",那不是这条用例想测的东西。"""
    case = case_from_turn("track-order",
                          Turn(user="帮我查下 ORD-1 的物流",
                               tools=["load_skill", "query_order"]),
                          known_tools=KNOWN)
    assert case["expected_tools"] == ["query_order"]


def test_turn_with_no_assertion_is_dropped():
    """断不出任何期望的用例**永远通过**,却每次门禁都要真跑一遍、花两次 token,
    还把通过率往上抬。它比没有这条用例更糟。"""
    assert case_from_turn("track-order", Turn(user="你们几点上班", tools=[]),
                          known_tools=KNOWN) is None


def test_scaffolding_turns_are_dropped():
    """**实测缺陷**。第一批合成结果里混进两条我自己走查时打的话:
    「加载 track-order 技能查 ORD-20240115-001 快递」。

    这种输入把答案写在题面上——它直接点名要加载哪个 skill,于是这条用例
    永远测不出"该不该选中这个 skill",而那正是门禁要测的东西。
    """
    assert case_from_turn("track-order",
                          Turn(user="加载 track-order 技能查 ORD-1 快递",
                               tools=["query_order"]), known_tools=KNOWN) is None


def test_too_short_turns_are_dropped():
    assert case_from_turn("track-order", Turn(user="好的", tools=["query_order"]),
                          known_tools=KNOWN) is None


def test_case_id_is_stable_so_resynthesis_is_idempotent():
    """id 只由买家原话决定——不含时间戳、不含序号。

    这是"重跑不产生重复用例"的全部依据。若 id 带时间戳,每跑一次 cron 就多一批
    实质相同的用例,门禁成本线性上涨而覆盖面一点没增加。
    """
    a = case_from_turn("track-order", Turn(user="帮我查下 ORD-1 的物流",
                                           tools=["query_order"]), known_tools=KNOWN)
    b = case_from_turn("track-order", Turn(user="帮我查下 ORD-1 的物流",
                                           tools=["query_logistics"]), known_tools=KNOWN)
    assert a["id"] == b["id"] and a["id"].startswith(SYNTH_ID_PREFIX)


def test_synthetic_ids_are_recognisable_without_reading_the_file():
    """case_id 会流到评测报告、门禁日志、界面。凡拿得到 id 的地方都要能判断
    这条有没有人审过——否则"未经人工审核"这个事实只存在于一个没人会去开的文件里。"""
    assert is_synthetic_case_id("synth-track-order-abc123")
    assert not is_synthetic_case_id("case-track-order-1")
    assert not is_synthetic_case_id("")


def test_description_carries_the_unaudited_label():
    """标注不能只在文件级:评测报告是一行行看过去的,没人会回头查这条来自哪个文件。"""
    case = case_from_turn("track-order", Turn(user="帮我查下 ORD-1 的物流",
                                              tools=["query_order"]), known_tools=KNOWN)
    assert "未经人工审核" in case["description"]


# --------------------------------------------------------------------------
# 采样源:强弱要分开
# --------------------------------------------------------------------------

def test_trace_sourced_sessions_win_and_are_labelled():
    archives = [_archive("s1", [_msg("user", "帮我查下 ORD-1 的物流"),
                                _msg("assistant", "好", tools=["query_order"])])]
    cases, stats = synthesize_gate_cases(
        "track-order", traces=[_trace("s1", "track-order")], archives=archives,
        known_tools=KNOWN)
    assert len(cases) == 1
    assert stats["from_trace"] == 1 and stats["from_keyword"] == 0
    assert stats["provenance"][0]["source"] == SOURCE_TRACE
    assert stats["provenance"][0]["session_id"] == "s1"


def test_keyword_source_covers_brand_new_skills_with_zero_traces():
    """**死结真正解开的那一支。** 新蒸馏的候选一条轨迹都没有——它还没上过线。

    只有轨迹这一个源的话,自动合成对"全新 skill"这个最需要它的场景毫无作用。
    """
    md = ('---\nname: order-query\n'
          'description: 查订单流程。关键词：查订单、我的订单\n---\n正文')
    archives = [_archive("s9", [_msg("user", "帮我查订单 ORD-1 到哪了"),
                                _msg("assistant", "好", tools=["query_order"])])]
    cases, stats = synthesize_gate_cases("order-query", traces=[], archives=archives,
                                         skill_md=md, known_tools=KNOWN)
    assert len(cases) == 1
    assert stats["from_keyword"] == 1
    assert stats["provenance"][0]["source"] == SOURCE_KEYWORD


def test_keyword_source_still_takes_assertions_only_from_facts():
    """关键词是**弱**证据,所以它只决定挑哪些会话,不参与任何一条断言。

    这一条是整个阶段一的安全边界:采样可以启发式,裁判尺不能。
    """
    md = '---\nname: order-query\ndescription: 关键词：查订单\n---\n正文'
    archives = [_archive("s9", [_msg("user", "帮我查订单 ORD-1"),
                                _msg("assistant", "好", tools=["list_user_orders"])])]
    cases, _ = synthesize_gate_cases("order-query", traces=[], archives=archives,
                                     skill_md=md, known_tools=KNOWN)
    # 断言的是那一轮**真实调用过**的工具,不是关键词暗示的任何东西
    assert cases[0]["expected_tools"] == ["list_user_orders"]


def test_canary_traces_do_not_produce_cases_for_the_live_skill():
    """灰度候选跑出来的轨迹属于那一版候选,拿它给现行版本造门禁用例是张冠李戴
    (与 `failure_cases.collect_failures_by_skill` 同一条口径)。"""
    archives = [_archive("s1", [_msg("user", "帮我查下 ORD-1 的物流"),
                                _msg("assistant", "好", tools=["query_order"])])]
    _, stats = synthesize_gate_cases(
        "track-order", traces=[_trace("s1", "track-order", variant="canary")],
        archives=archives, known_tools=KNOWN)
    assert stats["from_trace"] == 0


def test_human_reply_becomes_expected_keywords():
    """人工坐席回复是 human reference —— 论文里全部反馈信号的来源就是这种工单。"""
    human = json.dumps({"intent": "human_agent", "reply": "您这单是已发货状态,别急"},
                       ensure_ascii=False)
    archives = [_archive("s1", [_msg("user", "帮我查下 ORD-1 的物流"),
                                _msg("assistant", "好", tools=["query_order"]),
                                _msg("assistant", human)])]
    cases, _ = synthesize_gate_cases("track-order", traces=[_trace("s1", "track-order")],
                                     archives=archives, known_tools=KNOWN)
    assert "已发货" in cases[0].get("expected_keywords", [])


def test_same_utterance_across_sessions_yields_one_case():
    """同一句话被反复问是常态。5 条一模一样的用例:门禁为它们各付一次 token、
    一个偶发行为按 5 倍权重扭曲结论,而覆盖面一点没增加。"""
    msgs = [_msg("user", "帮我查下 ORD-1 的物流"),
            _msg("assistant", "好", tools=["query_order"])]
    cases, _ = synthesize_gate_cases(
        "track-order", traces=[_trace("s1", "track-order"), _trace("s2", "track-order")],
        archives=[_archive("s1", msgs), _archive("s2", list(msgs))], known_tools=KNOWN)
    assert len(cases) == 1


def test_max_cases_is_respected():
    msgs = [m for i in range(20) for m in
            (_msg("user", f"帮我查下 ORD-{i} 的物流"), _msg("assistant", "好", tools=["query_order"]))]
    cases, _ = synthesize_gate_cases("track-order", traces=[_trace("s1", "track-order")],
                                     archives=[_archive("s1", msgs)], known_tools=KNOWN,
                                     max_cases=3)
    assert len(cases) == 3


# --------------------------------------------------------------------------
# frontmatter 解析:关键词与 actor
# --------------------------------------------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("查订单流程。关键词：查订单、我的订单", ["查订单", "我的订单"]),
    ("退货流程。适用关键词：退货、退款、换货", ["退货", "退款", "换货"]),
    # **实测缺陷**:第一版只认冒号,`query-coupons` 写的是「关键词包括“优惠券”…」,
    # 于是被判成"未声明关键词"、合成 0 条 —— 它明明声明了。
    ('优惠券查询;关键词包括“优惠券”“有哪些券”“能领哪些”等', ["优惠券", "有哪些券", "能领哪些"]),
    ("没有声明关键词的描述", []),
])
def test_skill_keywords_parses_the_three_real_writing_styles(desc, expected):
    md = f"---\nname: x\ndescription: {desc}\n---\n正文"
    assert skill_keywords(md) == expected


def test_single_char_keywords_are_dropped():
    """一个字的"词"(如"退")会命中几乎所有会话,捞回来的样本与该 skill 无关。"""
    md = "---\nname: x\ndescription: 关键词：退、退货\n---\n正文"
    assert skill_keywords(md) == ["退货"]


def test_seller_skills_use_the_seller_tool_universe():
    """**实测缺陷**。`validator.known_tool_names()` 是**买家侧**的(它刻意减掉了
    `SELLER_ONLY_TOOLS`)。拿它去过滤 `daily-business-report` 这类卖家 skill,
    会把 `shop_overview` 之类全部当成"不存在的工具"剔掉 → 断言全空 → 用例全丢 →
    界面显示 0 条,而真实原因不是"没素材",是**用错了工具全集**。
    """
    from app.agent.tools.registry import SELLER_ONLY_TOOLS

    md = "---\nname: daily-business-report\nactor: seller\ndescription: 日报\n---\n正文"
    assert skill_actor(md) == "seller"
    assert SELLER_ONLY_TOOLS <= tool_universe("seller")
    assert not (SELLER_ONLY_TOOLS & tool_universe("buyer"))


def test_actor_defaults_to_buyer_for_unlabelled_and_bogus_values():
    assert skill_actor("---\nname: x\ndescription: d\n---\n") == "buyer"
    assert skill_actor("---\nname: x\nactor: 火星人\ndescription: d\n---\n") == "buyer"


# --------------------------------------------------------------------------
# 落盘:与人工集**物理分开**
# --------------------------------------------------------------------------

def test_synth_path_rejects_traversal_in_skill_name():
    """skill 名一路来自 LLM 生成的 frontmatter,素材是可被提示注入的顾客对话。
    这里要写文件,`..` 会让写入逃出目录(与 `build_shadow_dir` 同一条防线)。"""
    for bad in ("../../etc/passwd", "a/b", ".hidden", ""):
        with pytest.raises(ValueError):
            synth_path(bad, root="/tmp/x")


def test_save_and_load_roundtrip(tmp_path):
    cases = [{"id": "synth-x-1", "description": "自动合成·未经人工审核 | x | 查订单",
              "turns": ["帮我查下 ORD-1 的物流"], "related_skills": ["x"],
              "expected_tools": ["query_order"]}]
    path = save_synth_cases("x", cases, {"kept": 1}, root=tmp_path)
    loaded = load_synth_cases("x", root=tmp_path)
    assert [c.id for c in loaded] == ["synth-x-1"]
    assert loaded[0].expected_tools == ["query_order"]

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["synthetic"] is True
    assert "未经人工审核" in payload["note"]


def test_corrupt_synth_file_degrades_to_no_synthetic_cases(tmp_path):
    """合成集是**增量**能力。它坏掉时应该退回"只有人工用例"这个原状态,
    而不是让门禁连人工用例也跑不了——那会把一个增强变成一个新的单点故障。"""
    (tmp_path / "x.json").write_text("{ this is not json", encoding="utf-8")
    assert load_synth_cases("x", root=tmp_path) == []


def test_missing_file_is_not_an_error(tmp_path):
    assert load_synth_cases("never-synthesised", root=tmp_path) == []
