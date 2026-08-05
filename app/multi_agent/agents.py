"""领域画像配置:每个画像 = 专属 system prompt + 允许的工具子集。

编排器(orchestrator)按路由结果切换画像,用同一个硬化引擎(EcomAgent)执行——
不再各自维护一套 ReAct 循环(已统一到 EcomAgent,见 orchestrator.py)。

三域:售前(presale) / 售中(midsale) / 售后(aftersale,含原投诉)。
公共工具:每域都含 search_knowledge / recall_user_memory / save_user_memory /
load_skill / read_skill_file / read_tool_result。
"""

from app.prompts.agents import (AFTERSALE_BASE_PROMPT, AFTERSALE_PROMPT,
                                MIDSALE_BASE_PROMPT, MIDSALE_PROMPT,
                                PRESALE_BASE_PROMPT, PRESALE_PROMPT)

# 每个领域画像都具备的公共工具
_COMMON_TOOLS = {
    "search_knowledge", "recall_user_memory", "save_user_memory",
    "load_skill", "read_skill_file", "read_tool_result",
}


AGENT_CONFIGS = {
    "presale": {
        "name": "小夕-售前",
        "prompt": PRESALE_PROMPT,
        # base_prompt:不含风格头/安全规则的领域正文——orchestrator 每轮据此
        # 配店主当前语气重新拼接(见 build_profile_prompt);prompt 字段保留
        # 不变,以免破坏既有引用(CLI/评测沙箱等仍按默认语气使用它)。
        "base_prompt": PRESALE_BASE_PROMPT,
        "tools": _COMMON_TOOLS | {
            "query_product", "query_coupons", "negotiate_price", "list_user_orders",
            "place_order",   # 促成下单:创建待支付订单(不代付款)
        },
    },
    "midsale": {
        "name": "小夕-售中",
        "prompt": MIDSALE_PROMPT,
        "base_prompt": MIDSALE_BASE_PROMPT,
        "tools": _COMMON_TOOLS | {
            "query_order", "query_logistics", "expedite_shipping",
            "change_address", "cancel_order", "list_user_orders",
            "place_order",   # 创建待支付订单(不代付款)
        },
    },
    "aftersale": {
        "name": "小夕-售后",
        "prompt": AFTERSALE_PROMPT,
        "base_prompt": AFTERSALE_BASE_PROMPT,
        "tools": _COMMON_TOOLS | {
            "query_order", "query_logistics", "apply_refund",
            "issue_invoice", "list_user_orders",
        },
    },
}


# ---- B 端(卖家)画像:与买家画像结构一致,但工具子集完全不相交 ----
from app.prompts.seller_agents import ANALYST_PROMPT, GROWTH_PROMPT

# 卖家侧的公共工具:知识库 + skill 体系(不含任何买家记忆/买家写动作)
_SELLER_COMMON_TOOLS = {
    "search_knowledge", "load_skill", "read_skill_file", "read_tool_result",
}

SELLER_AGENT_CONFIGS = {
    "analyst": {
        "name": "参谋-小策",
        "prompt": ANALYST_PROMPT,
        "tools": _SELLER_COMMON_TOOLS | {
            "shop_overview", "product_diagnostics", "service_quality", "anomaly_scan",
            # N4:评价洞察(全店均分/差评率 + 差评 top 商品与关键词)。只读、
            # seller_only(见 registry.SELLER_ONLY_TOOLS),不出现在任何买家画像里。
            "review_insights",
        },
    },
    "growth": {
        "name": "增长-小拓",
        "prompt": GROWTH_PROMPT,
        "tools": _SELLER_COMMON_TOOLS | {
            "find_opportunities", "draft_outreach", "list_outreach_drafts",
            # 增长也要看经营面才能判断值不值得推
            "shop_overview", "product_diagnostics",
        },
    },
}
