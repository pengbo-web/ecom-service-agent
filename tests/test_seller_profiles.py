"""卖家画像:只读边界、买卖两侧工具不互穿、prompt 含硬约束。"""

from app.multi_agent.agents import AGENT_CONFIGS, SELLER_AGENT_CONFIGS
from app.agent.tools.registry import TOOL_DEFINITIONS

WRITE_TOOLS = {"apply_refund", "place_order", "cancel_order", "change_address",
               "expedite_shipping", "issue_invoice", "negotiate_price"}


def test_seller_configs_shape_matches_buyer():
    for key, cfg in SELLER_AGENT_CONFIGS.items():
        assert set(cfg) == {"name", "prompt", "tools"}
        assert isinstance(cfg["tools"], set) and cfg["tools"]
        assert cfg["prompt"].strip()


def test_analyst_is_read_only():
    """参谋碰到任何写工具 = 职责边界破了,也就是安全边界破了。"""
    assert SELLER_AGENT_CONFIGS["analyst"]["tools"] & WRITE_TOOLS == set()


def test_buyer_profiles_never_get_seller_tools():
    """经营数据绝不能进买家会话——买家问一句就能拿到全店 GMV 是数据泄漏。"""
    seller_only = {"shop_overview", "product_diagnostics", "service_quality",
                   "anomaly_scan", "find_opportunities", "draft_outreach",
                   "list_outreach_drafts"}
    for key, cfg in AGENT_CONFIGS.items():
        assert cfg["tools"] & seller_only == set(), f"{key} 混入了 B 端工具"


def test_seller_profiles_never_get_buyer_write_tools():
    for key, cfg in SELLER_AGENT_CONFIGS.items():
        assert cfg["tools"] & WRITE_TOOLS == set(), f"{key} 混入了买家写工具"


def test_all_declared_tools_are_registered():
    """画像里写了但注册表里没有的工具名,运行期会静默失效。"""
    known = {d["function"]["name"] for d in TOOL_DEFINITIONS}
    for key, cfg in SELLER_AGENT_CONFIGS.items():
        missing = cfg["tools"] - known
        assert missing == set(), f"{key} 声明了未注册的工具: {missing}"


def test_analyst_prompt_forbids_fabrication():
    p = SELLER_AGENT_CONFIGS["analyst"]["prompt"]
    assert "不要编造" in p or "不得编造" in p
    assert "只读" in p


def test_growth_prompt_states_draft_only():
    """营销 Agent 的 prompt 必须写死"只出草稿、不发送"。"""
    p = SELLER_AGENT_CONFIGS["growth"]["prompt"]
    assert "草稿" in p
    assert "人工" in p
