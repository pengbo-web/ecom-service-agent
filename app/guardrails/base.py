"""护栏公共结构。"""

from dataclasses import dataclass
from typing import Optional

# 输入被拦截时统一安全兜底（不泄露命中的具体规则）
SAFE_FALLBACK = (
    "抱歉，我只能协助与并夕夕购物相关的问题"
    "（订单查询、商品咨询、物流、退换货、售后等）。请问有什么可以帮您？"
)


@dataclass
class GuardResult:
    action: str            # "pass" | "block" | "sanitize"
    guard: str = ""
    reason: str = ""
    text: Optional[str] = None   # action=="sanitize" 时的处理后文本


class OutputGuard:
    """输出护栏基类：只声明分类标记，不强制继承（现有 SensitiveInfoGuard/
    ContactInfoGuard 是纯 duck-typing 类，不继承这里也能被 GuardPipeline
    正常使用）——`REWRITES_OUTPUT` 是这里唯一的强制契约，`PATTERNS` 是可选的
    补充契约。

    E1(回复流式化)读 `REWRITES_OUTPUT` 判断"这一轮的输出护栏会不会改写买家
    将看到的文本":
      - `REWRITES_OUTPUT = True`(变换类):`check()` 的 `action` 可能是
        "sanitize"，即会改写文本。
      - `REWRITES_OUTPUT = False`(观察/记录类，本仓库目前没有这类输出护栏，
        留给未来扩展):只读文本、只记埋点、不改 `text`，不影响流式吐字的
        安全性。

    E1b 进一步细分"变换类"内部——不是所有改写都同等危险:
      - 暴露 `PATTERNS`(一份编译后的正则列表)、且这些正则的最长匹配宽度能
        被 `app/guardrails/pattern_width.py` 静态算出来(不含无界重复)的
        变换类护栏，证明了它的改写只发生在正则匹配到的那一小段范围内——
        即"局部脱敏"。这类护栏可以安全套到逐块流式输出上：只要在吐字前扣住
        一段足够长(由 `PATTERNS` 推导出来，不是手写常数)的尾部缓冲区，就能
        保证"已经流出去的前缀"永远不会是本该被脱敏而没脱敏的原文(见
        `app/guardrails/streaming_redactor.py` 的正确性论证，以及
        `app/guardrails/pipeline.py` 的 `local_redaction_holdback`)。
      - 变换类但没有暴露 `PATTERNS`(或暴露了但宽度算不出来，比如正则本身
        无界)的护栏，视为"改写范围未知/可能是整段替换"——这一类存在时，
        这一轮必须整体退化为非流式(先全量生成→护栏跑完→一次性发)，因为
        "已经流出去的前缀"一旦事后被证明该改写而没改写，是没法收回的。

    输入侧护栏(PromptInjectionGuard)不在这个分类体系里:它在生成**之前**
    就能拦截(action=="block"),被拦的轮次根本不会进 ReAct 循环、更不会
    进流式分支,不存在"边流边改写"的冲突。
    """
    REWRITES_OUTPUT: bool = False
    # 可选:变换类护栏(REWRITES_OUTPUT=True)才需要设置——见上面 class docstring
    # "局部脱敏 vs 改写范围未知"之分。留空/不设 = 未证明局部脱敏。
    PATTERNS = None
