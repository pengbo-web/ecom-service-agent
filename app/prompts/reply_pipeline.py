"""回复流水线三提示词（H1.1）：评估器 / 重写 / 润色。

Phase H1 把「出话」拆成三段：主 Agent 先出草稿 → 评估器检查草稿是否「接地」
（即是否基于本轮工具的真实结果，不脱离事实）→ 不合格则重写 → 最终用「小夕」
人设润色语气。本文件只提供三段的系统/指令提示词字符串常量，不做任何拼接、
调用或选择逻辑（这些留给 H1.2 的 ReplyPipeline）。

三个常量都不含 `{xxx}` 花括号占位符：H1.2 会把用户问题/草稿/工具结果作为
对话消息（而非字符串插值）传给模型，本文件里用说明性文字交代「这是草稿」
「这是工具结果」即可。

提示词正文已外置到 prompts/customer_service/*.md。
"""

from prompts import get

EVALUATOR_PROMPT = get("customer_service/evaluator")
REDRAFT_PROMPT = get("customer_service/redraft")
POLISH_PROMPT = get("customer_service/polish")
SELECTOR_PROMPT = get("customer_service/selector")
