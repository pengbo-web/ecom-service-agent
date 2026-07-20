"""转人工升级判定（纯函数）。"""

# 敏感意图：命中即建议转人工（可按业务调整）
SENSITIVE_INTENTS = {"complaint"}


def should_escalate(intent: str, confidence: float, requires_human: bool,
                    threshold: float, sensitive_intents: set) -> list:
    """返回命中的升级原因列表；空列表表示无需转人工。"""
    reasons = []
    if requires_human:
        reasons.append("模型判定需转人工")
    if confidence < threshold:
        reasons.append(f"置信度过低({confidence:.2f} < {threshold})")
    if intent in sensitive_intents:
        reasons.append(f"敏感意图({intent})")
    return reasons
