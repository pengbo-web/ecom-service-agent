"""领域画像配置:每个画像 = 专属 system prompt + 允许的工具子集。

编排器(orchestrator)按路由结果切换画像,用同一个硬化引擎(EcomAgent)执行——
不再各自维护一套 ReAct 循环(已统一到 EcomAgent,见 orchestrator.py)。

三域:售前(presale) / 售中(midsale) / 售后(aftersale,含原投诉)。
公共工具:每域都含 search_knowledge / recall_user_memory / save_user_memory /
load_skill / read_skill_file / read_tool_result。
"""

from app.prompts.agents import AFTERSALE_PROMPT, MIDSALE_PROMPT, PRESALE_PROMPT

# 每个领域画像都具备的公共工具
_COMMON_TOOLS = {
    "search_knowledge", "recall_user_memory", "save_user_memory",
    "load_skill", "read_skill_file", "read_tool_result",
}


AGENT_CONFIGS = {
    "presale": {
        "name": "小夕-售前",
        "prompt": PRESALE_PROMPT,
        "tools": _COMMON_TOOLS | {
            "query_product", "query_coupons", "negotiate_price", "list_user_orders",
            "place_order",   # 促成下单:创建待支付订单(不代付款)
        },
    },
    "midsale": {
        "name": "小夕-售中",
        "prompt": MIDSALE_PROMPT,
        "tools": _COMMON_TOOLS | {
            "query_order", "query_logistics", "expedite_shipping",
            "change_address", "cancel_order", "list_user_orders",
            "place_order",   # 创建待支付订单(不代付款)
        },
    },
    "aftersale": {
        "name": "小夕-售后",
        "prompt": AFTERSALE_PROMPT,
        "tools": _COMMON_TOOLS | {
            "query_order", "query_logistics", "apply_refund",
            "issue_invoice", "list_user_orders",
        },
    },
}
