"""转人工升级判定（纯函数）。规则关键词 + LLM 意图/置信度混合。"""

import difflib
import re

# 敏感意图：命中即建议转人工（可按业务调整）
SENSITIVE_INTENTS = {"complaint"}

# 关键词兜底：用户明确表达投诉/维权时，不依赖 LLM 分类，直接转人工
ESCALATION_KEYWORDS = ["投诉", "差评", "曝光", "315", "消协", "工商", "起诉", "维权", "报警"]

# 明确要求人工的纯短句(前置短路用:整句锚定+长度上限,绝不误伤"人工审核要多久"这类正常问题)
HUMAN_REQUEST_PATTERNS = [
    re.compile(r"^(转人工|人工客服|找人工|叫?真人客服?|我要人工|人工服务)[\s!！~。.,，]*$"),
]

# 负面情绪词(文档5.2情绪识别前置/9.④):命中即升级转人工,回复照常给(安抚+banner,不短路)
ANGER_KEYWORDS = ["垃圾", "骗子", "气死", "太差劲", "什么破", "忽悠", "糊弄", "敷衍", "废物", "投诉你们"]


def match_human_fast(text: str) -> bool:
    """纯转人工请求判定(≤10字整句命中)——streaming 前置短路用,零 LLM 直接转接。"""
    t = (text or "").strip()
    if not t or len(t) > 10:
        return False
    return any(p.match(t) for p in HUMAN_REQUEST_PATTERNS)


def repeated_unresolved(user_input: str, prior_user_msgs: list,
                        times: int = 3, sim: float = 0.75) -> bool:
    """同一问题重复 times 次(含本次)判定:与既往用户消息相似度>=sim 的条数达 times-1。
    短语气词(<4字)不算问题,避免"好的好的"误判。"""
    t = (user_input or "").strip()
    if len(t) < 4:
        return False
    similar = 0
    for m in (prior_user_msgs or []):
        m = (m or "").strip()
        if len(m) < 4:
            continue
        if difflib.SequenceMatcher(None, t, m).ratio() >= sim:
            similar += 1
    return similar >= times - 1


def should_escalate(intent: str, confidence: float, requires_human: bool,
                    threshold: float, sensitive_intents: set,
                    user_input: str = "", qu_intent: str = "",
                    prior_user_msgs: list | None = None,
                    repeat_times: int = 3) -> list:
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
    # —— 转人工闭环(文档9.④)——
    if qu_intent == "转人工":
        reasons.append("用户明确要求转人工(查询理解)")
    elif qu_intent == "投诉":
        reasons.append("投诉倾向(查询理解)")
    if prior_user_msgs and repeated_unresolved(user_input, prior_user_msgs,
                                               times=repeat_times):
        reasons.append(f"同一问题重复{repeat_times}次未解决")
    anger = [k for k in ANGER_KEYWORDS if k in (user_input or "")]
    if anger:
        reasons.append(f"负面情绪({'、'.join(anger[:3])})")
    return reasons
