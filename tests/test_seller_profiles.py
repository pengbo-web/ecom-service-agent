"""卖家画像:只读边界、买卖两侧工具不互穿、prompt 含硬约束。

两条边界(参谋只读 / 买卖工具不互穿)不再在本文件里手抄工具名单——手抄的名单
只在"当时没漏"时才成立,新工具一加,名单和事实就悄悄分家(这正是
draft_outreach 曾经踩过的坑:它是状态可变的写工具,却不在旧的 WRITE_TOOLS
字面量里)。现在两条边界都从 app.agent.tools.registry 里的 TOOL_TRAITS 派生,
且有穷尽性测试兜底:_TOOL_MAP 里任何没打过标签的新工具都会让构建变红。
"""

from app.multi_agent.agents import AGENT_CONFIGS, SELLER_AGENT_CONFIGS
from app.agent.tools.registry import (
    _TOOL_MAP, BUYER_WRITE_TOOLS, MUTATING_TOOLS, SELLER_ONLY_TOOLS,
    TOOL_DEFINITIONS, TOOL_TRAITS,
)


def test_seller_configs_shape_matches_buyer():
    for key, cfg in SELLER_AGENT_CONFIGS.items():
        assert set(cfg) == {"name", "prompt", "tools"}
        assert isinstance(cfg["tools"], set) and cfg["tools"]
        assert cfg["prompt"].strip()


def test_analyst_is_read_only():
    """参谋碰到任何写工具 = 职责边界破了,也就是安全边界破了。"""
    assert SELLER_AGENT_CONFIGS["analyst"]["tools"] & MUTATING_TOOLS == set()


def test_buyer_profiles_never_get_seller_tools():
    """经营数据绝不能进买家会话——买家问一句就能拿到全店 GMV 是数据泄漏。"""
    for key, cfg in AGENT_CONFIGS.items():
        assert cfg["tools"] & SELLER_ONLY_TOOLS == set(), f"{key} 混入了 B 端工具"


def test_seller_profiles_never_get_buyer_write_tools():
    """卖家画像不该拿到*买家域*的写工具(apply_refund/cancel_order 等)。

    注意这里用 BUYER_WRITE_TOOLS(= MUTATING_TOOLS - SELLER_ONLY_TOOLS),而不是
    整个 MUTATING_TOOLS——growth 画像拥有 draft_outreach 是设计如此(它是
    mutating 但也是 seller_only,营销 Agent 的本职就是落草稿),不是越界。
    """
    for key, cfg in SELLER_AGENT_CONFIGS.items():
        assert cfg["tools"] & BUYER_WRITE_TOOLS == set(), f"{key} 混入了买家写工具"


def test_all_tool_map_entries_are_classified():
    """穷尽性兜底:_TOOL_MAP 里的每个工具都必须在 TOOL_TRAITS 里打过标签。

    没打标签的新工具不会静默地被当成"安全"放过——这条测试直接红,
    逼着加工具的人在 registry.py 里补一行分类。
    """
    unclassified = set(_TOOL_MAP) - set(TOOL_TRAITS)
    assert unclassified == set(), f"以下工具未在 TOOL_TRAITS 中分类: {unclassified}"


def test_all_declared_tools_are_registered():
    """画像里写了但注册表里没有的工具名,运行期会静默失效。"""
    known = {d["function"]["name"] for d in TOOL_DEFINITIONS}
    for key, cfg in SELLER_AGENT_CONFIGS.items():
        missing = cfg["tools"] - known
        assert missing == set(), f"{key} 声明了未注册的工具: {missing}"


def test_eval_sandbox_agent_gets_no_seller_tools(tmp_path):
    """【I4】边界不能只在**画像配置**上成立——还有第二条构造路径。

    评估沙箱的单 Agent 模式直接 `EcomAgent(...)`,不经过任何画像,拿到的是
    **全量注册表**:`shop_overview`(全店营收进评测轨迹)、以及
    `draft_outreach` —— 这条路径上唯一一个不在 admin 鉴权后面的卖家**写**工具。
    上面那几条只断言 AGENT_CONFIGS / SELLER_AGENT_CONFIGS,对它完全看不见。

    所以这里断言的是真正构造出来的 ToolManager 暴露了什么,而不是配置里写了什么。
    """
    from app.evaluation.sandbox import Sandbox

    agent = Sandbox(tmp_root=str(tmp_path))._build_agent(str(tmp_path / "s.json"))
    try:
        exposed = {d["function"]["name"] for d in agent.tool_manager.tool_definitions}
        assert exposed & SELLER_ONLY_TOOLS == set(), \
            f"评估沙箱把 B 端工具交给了买家 Agent: {exposed & SELLER_ONLY_TOOLS}"
        # 同时确认没有把买家工具也一起关掉(否则测试"通过"只是因为工具全空)
        assert "query_order" in exposed and "apply_refund" in exposed
    finally:
        try:
            agent.tool_manager.close()
        except Exception:
            pass


def test_analyst_prompt_forbids_fabrication():
    p = SELLER_AGENT_CONFIGS["analyst"]["prompt"]
    assert "不要编造" in p or "不得编造" in p
    assert "只读" in p


def test_analyst_prompt_states_no_per_variant_breakdown():
    """prompt 必须写死"本店数据没有按尺码/颜色/规格拆分的库存或销量"这条约束。

    背景:一次真实回答里,参谋在"没编造 GMV/退款率"之外,编出了一句
    "43/44/45 码共 12 双"——系统里 `products.stock` 只是一个整体数字,压根没有
    这张细分表。旧的 _NO_FABRICATION 只笼统禁止"编数字",没有点名"规格细分"
    这个具体缺口,才会在一处与真实数据相邻的细节上失守。

    本测试只能钉住"prompt 里写没写这条约束"这一件事,不能证明模型今后真的
    不会编——那要靠真实/回归对话去验证,prompt 文本测试保证不了行为。
    """
    p = SELLER_AGENT_CONFIGS["analyst"]["prompt"]
    assert "尺码" in p and "规格" in p
    assert "没有" in p and ("细分" in p or "拆分" in p)


def test_growth_prompt_states_draft_only():
    """营销 Agent 的 prompt 必须写死"只出草稿、不发送"。"""
    p = SELLER_AGENT_CONFIGS["growth"]["prompt"]
    assert "草稿" in p
    assert "人工" in p
