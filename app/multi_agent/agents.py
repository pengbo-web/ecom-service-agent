"""领域画像配置:每个画像 = 专属 system prompt + 允许的工具子集。

编排器(orchestrator)按路由结果切换画像,用同一个硬化引擎(EcomAgent)执行——
不再各自维护一套 ReAct 循环(已统一到 EcomAgent,见 orchestrator.py)。
"""

from app.prompts.agents import COMPLAINT_PROMPT, POSTSALE_PROMPT, PRESALE_PROMPT


AGENT_CONFIGS = {
    "presale": {
        "name": "小夕-售前",
        "prompt": PRESALE_PROMPT,
        "tools": {"query_product", "search_knowledge", "list_user_orders",
                  "query_coupons", "load_skill"},
    },
    "postsale": {
        "name": "小夕-售后",
        "prompt": POSTSALE_PROMPT,
        "tools": {
            "query_order", "query_logistics", "apply_refund", "list_user_orders",
            "cancel_order", "change_address", "expedite_shipping", "issue_invoice",
            "search_knowledge", "load_skill",
        },
    },
    "complaint": {
        "name": "小夕-投诉",
        "prompt": COMPLAINT_PROMPT,
        "tools": {"query_order", "query_logistics", "expedite_shipping",
                  "search_knowledge", "load_skill"},
    },
}
