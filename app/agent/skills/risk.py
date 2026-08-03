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

from app.agent.skills.loader import _parse_frontmatter
from app.agent.skills.validator import referenced_tools
from app.agent.skills.workflow import parse_workflow, referenced_workflow_tools

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

# 承诺类措辞:即使只引用了只读工具,正文承诺免运费/赔付一样有资金后果。
# 宁可误判成高危(多一次人工审核)也不能漏判——漏判会让让利承诺进入自动上线。
COMMITMENT_KEYWORDS = (
    "免运费", "免邮", "包邮", "退运费", "承担运费",
    "赔付", "先行赔付", "补偿", "全额退", "退差价", "返现",
    "包退", "优惠券补发", "补发优惠券", "代金券",
)

# 灰度 A/B 参数(low 档)
CANARY_PERCENT = 50
CANARY_MIN_SAMPLES = 10
CANARY_MAX_DROP = 0.1

# 绝对成功率看门狗参数(medium 档转正后)
ABSOLUTE_MIN_SAMPLES = 30
ABSOLUTE_MIN_RATE = 0.6


def _all_referenced_tools(content: str) -> set[str]:
    """候选引用的工具全集:正文反引号 + workflow 声明里的 tool/requires_tools。

    必须并上声明侧 —— workflow guards 正是本项目给"高危流程"用的惯用写法
    (见 definitions/process-return),若只扫正文,一个把 apply_refund 只写在
    guards 里的退款候选会被判成低危并进入自动上线,这是安全分级里最危险的漏判
    方向。口径与 validator.validate_candidate 保持一致。
    """
    return referenced_tools(content) | referenced_workflow_tools(
        parse_workflow(_parse_frontmatter(content)))


def classify_risk(content: str, is_new_skill: bool = False) -> str:
    """判定候选风险档(high/medium/low)。规则按顺序命中即返回。"""
    if _all_referenced_tools(content) & HIGH_RISK_TOOLS:
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
