"""工具运行时硬校验:关键参数在进业务逻辑前做格式校验,非法直接挡回并回传纠错。

借鉴 Customer-Agent 的 `send_goods_link` 护栏(函数体里 `if goods_id < 1000: 拒绝并纠错`):
把"参数捏造"从提示词软约束升级为**代码层硬防线**——LLM 编了假订单号(如历史上出现过的
YD/PX 开头、或把列表序号 1/2/3 当 ID),工具层直接返回纠错信息让它重新取真实值,
不进 DB 查询、不依赖模型自觉。校验通过返回 None。
"""

import re

# 真实订单号格式:ORD-8位日期-3位序号(如 ORD-20240110-003)
_ORDER_ID_RE = re.compile(r"^ORD-\d{8}-\d{3}$")

# 这些工具的 order_id 参数必须是真实订单号格式
_ORDER_ID_TOOLS = {
    "query_order", "query_logistics", "apply_refund",
    "cancel_order", "change_address",
}

_ORDER_ID_HINT = (
    "订单号格式不对。平台真实订单号形如 ORD-20240110-003(ORD- 开头 + 8位日期 + 3位序号)。"
    "请勿使用编造的单号或列表序号——如不确定,先调用 list_user_orders 获取用户的真实订单号,再重试。"
)


def validate_tool_args(name: str, arguments: dict) -> str | None:
    """校验工具参数;非法返回纠错字符串(供回传给 LLM),合法返回 None。"""
    if name in _ORDER_ID_TOOLS and "order_id" in arguments:
        oid = str(arguments.get("order_id") or "").strip()
        if not _ORDER_ID_RE.match(oid):
            return _ORDER_ID_HINT
    return None
