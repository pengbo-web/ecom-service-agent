"""参谋侧的三处"测量本身出错" —— 每一处都会让店主照着一个不存在的问题去行动。

参谋的全部价值建立在"它报的异常是真的"上。一次假警报的代价不是多看一眼,
是店主下次不再信它。本文件守的三件事都属于这一类:

1. 服务质量只统计**真实买家流量** —— 否则压测/走查数据会被报成线上异常;
2. 卖家侧的 ReAct 预算装得下 skill 声明的分析链 —— 否则流程永远走不完;
3. 「转人工」是买家侧概念,参谋提到这个**指标**不等于它转了人工。
"""

import pytest


# --------------------------------------------------------------------------
# ① 服务质量只看真实流量
# --------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    from app.db import Database

    d = Database(str(tmp_path / "t.db"))
    d.init_schema()
    monkeypatch.setattr("app.agent.tools.shop_analytics.get_db", lambda: d)
    return d


def test_service_quality_ignores_synthetic_traffic(db):
    """**实测撞到过,而且直接骗到了店主。**

    参谋在日报里写「track-order 工具失败率 100%,告警线 30%,建议立即核查技能
    配置」。查那 13 次失败:**13/13 全是 `dev`** —— 走查时反复查一个不属于当前
    用户的订单号,归属校验按设计拒绝。真实买家 0 条。

    看门狗那一侧早就按 live 过滤了,这一侧漏了。告警比自动回滚更需要挡:
    回滚至少还有可信下限兜着,而假警报是直接送到人眼前的。
    """
    from app.agent.runtime_context import SOURCE_DEV, SOURCE_LIVE, set_traffic_source
    from app.agent.tools.shop_analytics import service_quality

    set_traffic_source(SOURCE_LIVE)
    for i in range(9):
        db.record_skill_trace(f"L{i}", "1", "track-order", [], "success")
    set_traffic_source(SOURCE_DEV)
    for i in range(13):
        db.record_skill_trace(f"D{i}", "u1", "track-order",
                              [{"name": "query_order", "ok": False, "error": "未找到订单"}],
                              "tool_error")
    set_traffic_source(None)

    skills = {s["skill_name"]: s for s in service_quality(window_days=7)["skills"]}
    got = skills["track-order"]
    assert got["total"] == 9, f"合成流量混进服务质量统计了: {got}"
    assert got["tool_error_rate"] == 0.0, "假失败率会被 anomaly_scan 报成异常"


def test_service_quality_discloses_its_scope(db):
    """口径必须进**返回值**,不能只写在 docstring 里 —— 参谋只看得到工具返回的
    JSON。而且要说清"total=0 是没人用到它,不是它坏了"。"""
    from app.agent.tools.shop_analytics import service_quality

    scope = service_quality(window_days=7)["traffic_scope"]
    assert "live" in scope
    assert "不是它不工作" in scope


# --------------------------------------------------------------------------
# ② 卖家侧步数预算装得下它自己的 skill
# --------------------------------------------------------------------------

def test_seller_budget_covers_the_longest_declared_chain():
    """**声明的流程比预算长,流程就永远走不完** —— 而这不是模型的问题。

    实测(买家侧预算 3 步时):问「最近退款率是不是有问题」,参谋在第三步
    「差评佐证」处截断,店主得再追一句「继续」才补完归因报告。
    """
    from app.config.settings import settings

    # daily-business-report 声明四次工具,refund-attribution 是五步归因链
    assert settings.seller_max_react_steps >= 5, (
        "卖家 skill 声明的分析链最长五步,预算装不下就永远走不完")


def test_seller_orchestrator_actually_applies_it(tmp_path):
    """光加配置不算数,得真装到引擎上。"""
    from app.config.settings import settings
    from app.multi_agent.orchestrator import SellerOrchestrator

    o = SellerOrchestrator(session_path=str(tmp_path / "s.json"))
    assert o.engine.max_react_steps == settings.seller_max_react_steps


# --------------------------------------------------------------------------
# ③ 参谋提到「转人工率」不等于它转了人工
# --------------------------------------------------------------------------

def test_analyst_mentioning_the_handoff_metric_is_not_a_handoff():
    """**一个自指的怪圈,实测撞到了。**

    参谋的服务质量报告本来就要写「process-return 成功率 100%,转人工率 0%」,
    而「转人工」正好是「转人工率」的子串 —— 于是参谋一提到这个指标,这一轮就被
    记成"它自己转人工了",轨迹落 handoff,回头又被 service_quality 统计成卖家
    skill 的转人工率,那个数字再进入下一份报告。**一个指标把自己算了进去。**

    实测 refund-attribution 的转人工率被算成 0.50;当前没炸只是因为
    anomaly_scan 有 min_samples=5 兜着。
    """
    from app.agent.chat import EcomAgent
    from app.agent.runtime_context import ACTOR_SELLER, set_current_actor

    report = "所有技能执行成功率均正常(process-return 成功率100%,转人工率0%)"
    set_current_actor(ACTOR_SELLER)
    try:
        assert EcomAgent._requires_human_from_text(report) is False
    finally:
        set_current_actor(None)


def test_buyer_side_handoff_detection_is_untouched():
    """买家侧那条兜底话术必须照旧命中 —— 这才是这个判据存在的理由。"""
    from app.agent.chat import EcomAgent
    from app.agent.runtime_context import ACTOR_BUYER, set_current_actor

    set_current_actor(ACTOR_BUYER)
    try:
        assert EcomAgent._requires_human_from_text("这个问题我帮您转人工客服处理") is True
        assert EcomAgent._requires_human_from_text("您的订单已发货") is False
    finally:
        set_current_actor(None)


# --------------------------------------------------------------------------
# ④ 关键词解析只有一份实现
# --------------------------------------------------------------------------

def test_all_seller_skills_are_reachable_by_deterministic_preload():
    """**这条是根因。**

    matcher 原来只认「适用关键词：」,而三个卖家 skill 写的是「关键词：」——
    抽出来 0 个词 → match_skill 永不命中 → 服务端预加载对卖家侧完全不生效 →
    参谋只能靠模型自觉调 load_skill,而 matcher 模块开头那段话说的就是它不会自觉。

    连锁后果:skill 的报告模板不生效、硬约束不生效、**卖家侧 skill 永远没有
    执行轨迹**(这正是"四个卖家 skill 门禁用例 0 条"的根因)。
    """
    from pathlib import Path

    from app.agent.skills.loader import _parse_frontmatter
    from app.agent.skills.matcher import extract_keywords

    root = Path("app/agent/skills/definitions")
    if not root.is_dir():
        pytest.skip("本机没有技能目录")
    missing = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or not (d / "SKILL.md").exists():
            continue
        meta = _parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
        if not extract_keywords(str(meta.get("description") or "")):
            missing.append(d.name)
    assert not missing, f"这些 skill 抽不出关键词,永远不会被预加载: {missing}"


@pytest.mark.parametrize("desc,expected", [
    ("退货流程。适用关键词：退货、退款", ["退货", "退款"]),
    ("经营日报。关键词：日报、周报", ["日报", "周报"]),
    ('优惠券;关键词包括“优惠券”“有哪些券”等', ["优惠券", "有哪些券"]),
    ("没有声明关键词的描述", []),
])
def test_one_parser_for_all_three_writing_styles(desc, expected):
    """曾经两处各写一份解析器,各修各的 —— 同一个 skill 在"预加载"和"合成门禁
    用例"两条路上被解析出不同的关键词。现在只有 matcher 一份。"""
    from app.agent.skills.matcher import extract_keywords

    assert extract_keywords(desc) == expected


def test_case_synthesis_reuses_the_same_parser():
    from app.agent.skills.case_synthesis import skill_keywords
    from app.agent.skills.matcher import extract_keywords

    md = "---\nname: x\ndescription: 日报。关键词：日报、周报\n---\n正文"
    assert skill_keywords(md) == extract_keywords("日报。关键词：日报、周报")
