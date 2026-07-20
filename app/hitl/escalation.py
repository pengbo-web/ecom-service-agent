"""转人工升级判定（纯函数）。规则关键词 + LLM 意图/置信度混合。"""

# 敏感意图：命中即建议转人工（可按业务调整）
SENSITIVE_INTENTS = {"complaint"}

# 关键词兜底：用户明确表达投诉/维权时，不依赖 LLM 分类，直接转人工
ESCALATION_KEYWORDS = ["投诉", "差评", "曝光", "315", "消协", "工商", "起诉", "维权", "报警"]


def should_escalate(intent: str, confidence: float, requires_human: bool,
                    threshold: float, sensitive_intents: set,
                    user_input: str = "") -> list:
    """返回命中的升级原因列表；空列表表示无需转人工。"""
    reasons = []
    if requires_human:
        reasons.append("模型判定需转人工")
    if confidence < threshold:
        reasons.append(f"置信度过低({confidence:.2f} < {threshold})")
    if intent in sensitive_intents:
        reasons.append(f"敏感意图({intent})")
    hit = [k for k in ESCALATION_KEYWORDS if k in (user_input or "")]
    if hit:
        reasons.append(f"用户明确要求投诉/维权（关键词: {'、'.join(hit)}）")
    return reasons
