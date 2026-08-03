"""分级授权:按候选引用的工具与承诺类措辞自动判风险档,决定放行方式。

一刀切人工确认过度保守(改个话术也要人批),一刀切自动又会让"自我改写退款流程"
直接造成资金风险。所以按静态特征分三档:

- low    改进已有 skill 且只用只读工具 → 真 A/B 灰度,不劣化即自动转正;
- medium 全新 skill(线上原本没有它,**没有对照组**) → 过离线门禁即转正,之后按
         绝对成功率看门狗守着;
- high   引用退款/议价/取消/改址/开票/催发货,或正文承诺免运费/赔付 → 永远人工。

判定只看静态文本,不需要跑起来,故可在转正前离线算出。
"""

from __future__ import annotations

from app.agent.skills.validator import referenced_tools

RISK_HIGH = "high"
RISK_MEDIUM = "medium"
RISK_LOW = "low"

POLICY_CANARY_AB = "canary_ab"              # 灰度 A/B:与现行版比成功率
POLICY_GATE_THEN_WATCH = "gate_then_watch"  # 过离线门禁即转正 + 绝对成功率看门狗
POLICY_MANUAL = "manual"                    # 永远人工确认

# 碰钱/改单/开票类工具:引用即高危——自我改写这些流程会直接造成资金与承诺风险
HIGH_RISK_TOOLS = frozenset({
    "apply_refund", "cancel_order", "change_address",
    "negotiate_price", "issue_invoice", "expedite_shipping",
})

# 承诺类措辞:即使只引用了只读工具,正文承诺免运费/赔付一样有资金后果
COMMITMENT_KEYWORDS = (
    "免运费", "免邮", "赔付", "补偿", "全额退", "承担运费", "包退", "先行赔付",
)

# 灰度 A/B 参数(low 档)
CANARY_PERCENT = 50
CANARY_MIN_SAMPLES = 10
CANARY_MAX_DROP = 0.1

# 绝对成功率看门狗参数(medium 档转正后)
ABSOLUTE_MIN_SAMPLES = 30
ABSOLUTE_MIN_RATE = 0.6


def classify_risk(content: str, is_new_skill: bool = False) -> str:
    """判定候选风险档(high/medium/low)。规则按顺序命中即返回。"""
    if referenced_tools(content) & HIGH_RISK_TOOLS:
        return RISK_HIGH
    if any(kw in content for kw in COMMITMENT_KEYWORDS):
        return RISK_HIGH
    if is_new_skill:
        return RISK_MEDIUM
    return RISK_LOW


def promotion_policy(risk: str) -> str:
    """该档的放行方式;未知档位一律按最保守的人工处理(fail-closed)。"""
    return {
        RISK_LOW: POLICY_CANARY_AB,
        RISK_MEDIUM: POLICY_GATE_THEN_WATCH,
    }.get(risk, POLICY_MANUAL)
