"""工具注册表：OpenAI function calling schema + 分发执行"""

import json
from typing import Callable

from app.agent.tools.order import query_order
from app.agent.tools.product import query_product
from app.agent.tools.logistics import query_logistics
from app.agent.tools.refund import apply_refund
from app.agent.tools.knowledge import search_knowledge
from app.agent.tools.user_orders import list_user_orders
from app.agent.tools.memory_tool import recall_user_memory, save_user_memory
from app.agent.tools.skill_tool import load_skill, read_skill_file
from app.config.settings import settings
from app.agent.tools.bargain import negotiate_price
from app.agent.tools.read_result import read_tool_result
from app.agent.tools.order_ops import (
    change_address, cancel_order, expedite_shipping, issue_invoice, query_coupons,
)
from app.agent.tools.shop_analytics import (
    shop_overview, product_diagnostics, service_quality,
)
from app.agent.tools.anomaly import anomaly_scan
from app.agent.tools.growth import (
    find_opportunities, draft_outreach, list_outreach_drafts_tool,
)

_TOOL_MAP: dict[str, Callable] = {
    "query_order": query_order,
    "query_product": query_product,
    "query_logistics": query_logistics,
    "apply_refund": apply_refund,
    "search_knowledge": search_knowledge,
    "list_user_orders": list_user_orders,
    "recall_user_memory": recall_user_memory,
    "save_user_memory": save_user_memory,
    "load_skill": load_skill,
    "read_skill_file": read_skill_file,
    "read_tool_result": read_tool_result,
    "change_address": change_address,
    "cancel_order": cancel_order,
    "expedite_shipping": expedite_shipping,
    "issue_invoice": issue_invoice,
    "query_coupons": query_coupons,
    "shop_overview": shop_overview,
    "product_diagnostics": product_diagnostics,
    "service_quality": service_quality,
    "anomaly_scan": anomaly_scan,
    "find_opportunities": find_opportunities,
    "draft_outreach": draft_outreach,
    "list_outreach_drafts": list_outreach_drafts_tool,
}

if settings.bargain_enabled:
    _TOOL_MAP["negotiate_price"] = negotiate_price

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "query_order",
            "description": "根据订单号查询订单详情，包括订单状态、商品信息、金额、物流单号等",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "订单号，例如 ORD-20240115-001",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_product",
            "description": "根据商品名称关键词或商品ID查询商品信息，包括价格、库存、规格等。支持模糊搜索",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "商品名称关键词或商品ID，例如「耳机」「运动鞋」「SHOE-270-BK-42」",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_logistics",
            "description": "根据订单号查询物流轨迹信息，包括快递公司、运单号、运输状态和轨迹事件",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "订单号，例如 ORD-20240115-001",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "检索并夕夕的政策与帮助文档（退换货政策、配送说明、会员权益、常见问题 FAQ）。"
                "当顾客询问规则、流程、时效、是否支持等政策类问题时使用，"
                "比如「能退货吗」「多久到账」「钻石会员有什么权益」「偏远地区包邮吗」。"
                "返回 Top-K 命中片段及来源文档，请基于检索结果回答，不要编造政策"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "用顾客的原问题或一句简洁中文描述要查的政策点",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回片段数，默认 3，最大 5",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_user_orders",
            "description": (
                "查询当前用户的所有订单概要列表（订单号、状态、商品、金额、下单时间）。"
                "当用户想查订单但未提供订单号，或提供的订单号查不到时，"
                "调用此工具列出订单供用户确认"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_refund",
            "description": (
                "当用户想退款时调用本工具。系统会强制前置确认:若返回 "
                "need_confirm=true,请把其中的确认问题转达给用户,让其确认后你再次调用本工具即可;"
                "无需你自己纠结是否已确认——由系统门控。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "要退款的订单号",
                    },
                    "reason": {
                        "type": "string",
                        "description": "退款原因，例如「尺码不合适」「质量问题」「不想要了」",
                    },
                },
                "required": ["order_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_user_memory",
            "description": (
                "查询当前用户的记忆信息，包括本次对话提取的短期记忆和跨会话的长期记忆。"
                "当需要回顾用户的偏好、历史问题、会员信息等时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "可选的查询关键词，用于过滤记忆内容",
                        "default": "",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_user_memory",
            "description": (
                "把用户明确表达的个人偏好、身份信息或重要事实即时写入长期记忆"
                "（跨会话永久生效）。仅当用户清晰说出关于自己的事实时使用，"
                "例如「我喜欢红色」「我对海鲜过敏」「以后都发顺丰」。"
                "闲聊内容、你的猜测、未经用户确认的信息不要写入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的事实，用第三人称简洁陈述，如「用户偏好红色衣服」",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["identity", "preference", "behavior", "issue", "other"],
                        "description": "事实类别：identity=身份/会员，preference=偏好，behavior=行为习惯，issue=问题记录，other=其他",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "加载指定技能的完整操作指令。"
                "当用户问题匹配某个可用技能时，调用此工具获取该技能的详细处理流程，"
                "然后按流程指引使用已有工具完成用户请求。"
                "可用技能会在系统提示中列出。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "要加载的技能名称，如 process-return、track-order、product-recommend",
                    }
                },
                "required": ["skill_name"],
            },
        },
    },
]

_NEGOTIATE_PRICE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "negotiate_price",
        "description": (
            "当买家就某商品砍价、要求折扣/优惠或提出一个具体价格时调用，进行一轮议价。"
            "调用前必须先确定商品的 product_id（可用 query_product 查询）。"
            "买家报了具体价格就填 buyer_offer（单位：元）；只说\"便宜点\"没给数字则省略 buyer_offer。"
            "工具返回 decision（accept/counter/reject）与 suggested_price，"
            "请据此组织话术，切勿报出低于 suggested_price 的价格，也不要透露底价。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "string",
                    "description": "要议价的商品ID，如 SHOE-270-BK-42",
                },
                "buyer_offer": {
                    "type": "number",
                    "description": "买家出价（元），未报具体数字时省略",
                },
            },
            "required": ["product_id"],
        },
    },
}

TOOL_DEFINITIONS.extend([
    {
        "type": "function",
        "function": {
            "name": "change_address",
            "description": (
                "修改订单的收货地址(仅未发货订单)。敏感操作:系统强制前置确认——"
                "若返回 need_confirm=true,请把确认问题转达用户,其确认后你再次调用本工具即可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "要修改的订单号"},
                    "new_address": {"type": "string", "description": "新的完整收货地址"},
                },
                "required": ["order_id", "new_address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": (
                "取消订单(仅未发货订单)。敏感操作:系统强制前置确认——"
                "若返回 need_confirm=true,请把确认问题转达用户,其确认后你再次调用本工具即可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "要取消的订单号"},
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expedite_shipping",
            "description": "为订单催发货/加急处理。当用户着急、催单时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "要加急的订单号"},
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "issue_invoice",
            "description": "为已完成支付的订单开具电子发票。用户要发票/报销凭证时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "要开票的订单号"},
                    "title": {"type": "string", "description": "发票抬头(个人或公司名)", "default": "个人"},
                    "tax_id": {"type": "string", "description": "公司税号(个人抬头可省略)", "default": ""},
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_coupons",
            "description": (
                "查询当前用户【可领】的优惠券,已按其会员等级与新老客身份筛选。"
                "无需参数(用户身份由服务端上下文确定)。返回 coupons(可领)与 "
                "unavailable(不可领及原因);请只向用户介绍 coupons 里的券,"
                "unavailable 仅供你判断,不要承诺用户能用不可领的券。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
])

if settings.bargain_enabled:
    TOOL_DEFINITIONS.append(_NEGOTIATE_PRICE_SCHEMA)

TOOL_DEFINITIONS.append({
    "type": "function",
    "function": {
        "name": "read_tool_result",
        "description": (
            "读回此前因过长被存档的工具结果。当某个工具结果里出现 "
            "`truncated: true` 与 `result_ref` 时,若预览不足以回答,用本工具按 ref 读取完整内容;"
            "内容很大时可用 offset/length 分段多次读取。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "结果引用,如 tr_a1b2c3d4e5"},
                "offset": {"type": "integer", "description": "起始字符偏移,默认 0", "default": 0},
                "length": {"type": "integer", "description": "读取字符数,默认 4000", "default": 4000},
            },
            "required": ["ref"],
        },
    },
})

TOOL_DEFINITIONS.append({
    "type": "function",
    "function": {
        "name": "read_skill_file",
        "description": (
            "读取某个技能附带的参考资料(如 references/xxx.md)。"
            "load_skill 的返回里若列出了「附带的参考资料」,需要哪份就用本工具取哪份——"
            "不要一次把所有资料都读进来。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "技能名,如 process-return"},
                "file": {"type": "string",
                         "description": "技能目录内的相对路径,取自 load_skill 列出的清单"},
            },
            "required": ["skill_name", "file"],
        },
    },
})

TOOL_DEFINITIONS.extend([
    {
        "type": "function",
        "function": {
            "name": "shop_overview",
            "description": "【店铺参谋专用】查询店铺经营总览：订单量、GMV、客单价、退款率、取消率、咨询会话数。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_days": {"type": "integer",
                                    "description": "统计窗口天数，默认 7"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "product_diagnostics",
            "description": "【店铺参谋专用】按商品诊断：下单量、销售额、退款率、退款原因 top3、库存。按最疼的商品排序。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_days": {"type": "integer", "description": "统计窗口天数，默认 7"},
                    "top_n": {"type": "integer", "description": "返回商品数，默认 5"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_quality",
            "description": "【店铺参谋专用】按技能统计服务质量：执行成功率、工具失败率、转人工率。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_days": {"type": "integer", "description": "统计窗口天数，默认 7"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "anomaly_scan",
            "description": "【店铺参谋专用】按确定性阈值扫描经营与服务异常（退款率/工具失败率/转人工率），返回跨线条目。只读，不调用大模型。",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_days": {"type": "integer", "description": "统计窗口天数，默认 7"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_opportunities",
            "description": "【营销增长专用】按类型查找被漏掉的成交机会。kind: stale_pending_order(下单后久未推进) / stalled_bargain(议价未成交) / consulted_no_order(咨询过没下单)。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string",
                             "enum": ["stale_pending_order", "stalled_bargain", "consulted_no_order"],
                             "description": "商机类型"},
                    "window_days": {"type": "integer", "description": "回看天数，默认 14"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
                },
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_outreach",
            "description": "【营销增长专用】为某个商机生成一条触达话术【草稿】，落入待审队列。注意：这只是草稿，不会发送给买家，必须由店主人工批准后才会发出。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "目标买家的 user_id"},
                    "content": {"type": "string", "description": "触达话术正文，简短口语，3 句以内"},
                    "kind": {"type": "string",
                             "enum": ["stale_pending_order", "stalled_bargain", "consulted_no_order"]},
                    "order_id": {"type": "string", "description": "相关订单号（如有）"},
                    "reason": {"type": "string", "description": "为什么触达这个人（给店主看的理由）"},
                    "offer_note": {"type": "string", "description": "建议的优惠说明（不是承诺，需店主确认）"},
                },
                "required": ["user_id", "content", "kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_outreach_drafts",
            "description": "【营销增长专用】查看触达草稿及其审批状态（draft/approved/rejected/sent）。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "按状态过滤，默认 draft"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
                },
                "required": [],
            },
        },
    },
])


# 有副作用的写工具:执行前查幂等键,成功后写幂等键(防重复副作用)。
# 注意:negotiate_price 不在此列——它的报价依赖 DB 里逐轮递减的议价轮次 rounds,而幂等键
# 只含 {product_id, buyer_offer} 不含 rounds;若纳入,买家反复"便宜点"(args 相同)会命中
# 首次缓存的 counter 价、bump_bargain_state 不再执行,阶梯让价被永久冻结(redis 幂等下)。
# 议价的多轮推进本就是期望行为,不属于"需去重的副作用",故排除。
_WRITE_TOOLS = frozenset({"apply_refund", "cancel_order", "change_address"})


def execute_tool(name: str, arguments: dict) -> str:
    """根据工具名称分发执行，返回 JSON 字符串结果。写工具带幂等保护(R6)。"""
    func = _TOOL_MAP.get(name)
    if not func:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

    # 运行时硬校验:非法参数(如编造的订单号)在进业务逻辑前挡回并回传纠错
    from app.agent.tools.validation import validate_tool_args
    _verr = validate_tool_args(name, arguments)
    if _verr is not None:
        return json.dumps({"success": False, "error": _verr}, ensure_ascii=False)

    idem = None
    if name in _WRITE_TOOLS:
        from app.session.idempotency import get_idempotency_store, idempotency_key
        from app.agent.tools.bargain import get_current_session
        store = get_idempotency_store()
        key = idempotency_key(get_current_session(), name, arguments)
        cached = store.get(key)
        if cached is not None:
            return cached          # 已成功执行过 → 返回缓存,不重复副作用
        idem = (store, key)

    try:
        result = func(**arguments)
    except Exception as e:
        result = {"error": f"工具执行出错: {e}"}
    out = json.dumps(result, ensure_ascii=False)

    if idem is not None and isinstance(result, dict) and result.get("success"):
        idem[0].put(idem[1], out)   # 仅缓存成功的写结果
    return out
