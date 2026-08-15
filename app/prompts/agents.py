"""Multi-Agent 模式下各领域画像的专属 Prompt。

三个领域画像（售前 presale / 售中 midsale / 售后 aftersale）的 system prompt，
以及 Router 的意图分类 prompt。售后画像并入了原「投诉」的情绪安抚话术。

（保留旧常量别名 POSTSALE_PROMPT / COMPLAINT_PROMPT 以兼容其它引用；
AGENT_CONFIGS 已改用新的三域常量。）

**N1 品牌语气可配置**:风格头不再在模块导入时被硬编码进三个域常量的固定文案——
`build_profile_prompt()` 把"风格块(店主可控)"当参数拼进来，PRESALE_PROMPT 等
常量仍在导入时用**默认**风格块拼好(供 CLI/评测沙箱按默认语气使用)，但真正服务
买家的编排器(`orchestrator.py::MultiAgentOrchestrator.chat()`)每轮用
`app.config.shop_profile.load_profile()` 读店主当前设置重新拼一份。
"""

from app.config import shop_profile as _sp
from prompts import get as _get_prompt

# 不代客下单(硬规则,禁止违反)。这是**安全规则**,拼接顺序上必须晚于店主可控
# 的风格块——见下面 build_profile_prompt 的顺序说明。对外公开为 SAFETY_RULES
# (原名 _NO_ORDER,内容不变)供拼接顺序测试断言。
SAFETY_RULES = _get_prompt("customer_service/safety_rules")

# 默认风格块(店主未自定义时使用):用空 profile 渲染,render_style_block 内部
# 会回落到 shop_profile.DEFAULT_TONE / DEFAULT_SHOP_NAME。不走 load_profile()——
# 那个函数会读数据库,而这里是模块导入时执行的常量拼接,不该在 import 阶段碰 DB。
_DEFAULT_STYLE_BLOCK = _sp.render_style_block({})


def build_profile_prompt(base_prompt: str, style_block: str) -> str:
    """组装一份画像的 system prompt。

    **顺序即安全边界**:风格块(店主可控) → 安全规则 → 领域正文。安全规则必须在
    店主文本之后,后写的指令优先级更高,店主无法用语气设定豁免"不代客下单/
    不许编造"这类底线。改这个顺序等于把店主输入提到底线之上,不要改。
    """
    return style_block + SAFETY_RULES + base_prompt


ROUTER_PROMPT = _get_prompt("routing/buyer_router")


PRESALE_PROMPT = _get_prompt("buyer_profiles/presale")


MIDSALE_PROMPT = _get_prompt("buyer_profiles/midsale")


AFTERSALE_PROMPT = _get_prompt("buyer_profiles/aftersale")


# base_prompt:不含风格头/安全规则的领域正文——供 orchestrator 每轮按店主当前
# 语气重新拼接(AGENT_CONFIGS[k]["base_prompt"] 直接引用这三个常量)。
PRESALE_BASE_PROMPT = PRESALE_PROMPT
MIDSALE_BASE_PROMPT = MIDSALE_PROMPT
AFTERSALE_BASE_PROMPT = AFTERSALE_PROMPT

# 给三个画像统一拼上"默认风格块 + 安全规则"(= build_profile_prompt(原文, 默认风格块))。
# 保留这三个常量本身不变,供 CLI / 评测沙箱按默认语气使用；运行时的真实拼接见
# orchestrator.py,那里用店主当前配置的风格块代替 _DEFAULT_STYLE_BLOCK。
PRESALE_PROMPT = build_profile_prompt(PRESALE_BASE_PROMPT, _DEFAULT_STYLE_BLOCK)
MIDSALE_PROMPT = build_profile_prompt(MIDSALE_BASE_PROMPT, _DEFAULT_STYLE_BLOCK)
AFTERSALE_PROMPT = build_profile_prompt(AFTERSALE_BASE_PROMPT, _DEFAULT_STYLE_BLOCK)

# ---- 向后兼容别名（旧代码/测试可能仍引用；语义已并入新三域）----
POSTSALE_PROMPT = AFTERSALE_PROMPT
COMPLAINT_PROMPT = AFTERSALE_PROMPT
