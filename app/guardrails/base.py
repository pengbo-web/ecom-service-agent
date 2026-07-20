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
