"""B 端(卖家)两个 Agent 的专属 Prompt:店铺参谋 / 营销增长。

提示词正文已外置到 prompts/seller_profiles/，本模块保留组装逻辑。
与 C 端画像的根本差别:说话对象是**店主**不是买家,可以直接给数字、给判断、
给可执行建议,不需要客服话术。但两条硬约束必须写死在 prompt 里:
- 参谋:全只读 + 不许编数字(没工具返回就说没有)
- 营销:只出草稿 + 发送必须人工批准
"""

from prompts import get

_NO_FABRICATION = get("seller_profiles/no_fabrication")


ANALYST_PROMPT = get("seller_profiles/analyst").replace(
    "{{_NO_FABRICATION}}", _NO_FABRICATION
)


def _kind_lines() -> str:
    """把商机类型清单从 `growth.OPPORTUNITY_KINDS` **渲染**进 prompt,不手抄。

    延迟到函数内 import:prompts 是被 app.multi_agent.agents 在导入期拉起来的,
    直接在模块顶层 import app.agent.tools.growth 会把 db/settings 一整条依赖
    链提前拽进 prompt 模块。
    """
    from app.agent.tools.growth import OPPORTUNITY_KINDS
    return "\n".join(f"  - `{k}`:{v}" for k, v in OPPORTUNITY_KINDS.items())


GROWTH_PROMPT = get("seller_profiles/growth").replace(
    "{{_kind_lines()}}", "\n" + _kind_lines() + "\n"
).replace(
    "{{_NO_FABRICATION}}", _NO_FABRICATION
)


SELLER_ROUTER_PROMPT = get("routing/seller_router")
