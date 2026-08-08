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
    正常使用）——`REWRITES_OUTPUT` 是这里唯一的契约。

    E1(回复流式化)读这个标记决定"这一轮能不能流式吐字":
      - `REWRITES_OUTPUT = True`(变换类):`check()` 的 `action` 可能是
        "sanitize"，即**会改写买家将看到的文本**——ContactInfoGuard 命中时
        甚至是整段替换（不是局部脱敏），意味着"已经流出去的前缀"事后无法
        再收回，唯一安全的做法是这一类护栏存在时压根不逐块吐字，等全量生成
        完、护栏跑完再一次性发（见 app/api/streaming.py 与 app/agent/chat.py
        `_can_stream_first_step` 的联动）。
      - `REWRITES_OUTPUT = False`(观察/记录类，本仓库目前没有这类输出护栏，
        留给未来扩展):只读文本、只记埋点、不改 `text`，不影响流式吐字的
        安全性。

    输入侧护栏(PromptInjectionGuard)不在这个分类体系里:它在生成**之前**
    就能拦截(action=="block"),被拦的轮次根本不会进 ReAct 循环、更不会
    进流式分支,不存在"边流边改写"的冲突。
    """
    REWRITES_OUTPUT: bool = False
