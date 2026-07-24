"""输出侧护栏：敏感信息脱敏 + 联系方式外流拦截。"""

import re

from app.guardrails.base import GuardResult

_RE_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_RE_ID = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
_RE_BANK = re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)")
_RE_EMAIL = re.compile(r"([\w.+-]{1,3})[\w.+-]*@([\w-]+\.[\w.-]+)")

_CONTACT_PATTERNS = [
    # 只拦「引导站外联系」:加/留微信、微信号、微信联系……
    # 单纯提及支付渠道(「支付方式:微信/支付宝/银行卡」「原路退回」)是正当客服话术,不拦。
    # 教训:旧写法 r"(加|上|留个?)?微信(?!支付|付款)" 前缀可选=裸词「微信」即命中,
    # 把整条正确的退款说明替换成了安全提醒(用户什么都看不到)。前缀必须必选。
    r"(加|上|留个?|换|发|给)微信", r"微信号", r"微信.{0,4}(联系|详聊|私聊)",
    r"\bweixin\b", r"\bwechat\b", r"\bQQ\b",
    r"私(下|聊).{0,4}(交易|联系|发)", r"线下(交易|付款|联系)", r"绕过平台",
]
_CONTACT_COMPILED = [re.compile(p, re.IGNORECASE) for p in _CONTACT_PATTERNS]

_CONTACT_REMINDER = "【安全提醒】为保障您的权益，请通过并夕夕平台内沟通与交易。"


class SensitiveInfoGuard:
    name = "sensitive_info"

    def check(self, text: str) -> GuardResult:
        original = text or ""
        masked = _RE_PHONE.sub(r"\1****\2", original)
        masked = _RE_ID.sub(r"\1********\2", masked)
        masked = _RE_BANK.sub(r"\1****\2", masked)
        masked = _RE_EMAIL.sub(r"\1***@\2", masked)
        if masked != original:
            return GuardResult(action="sanitize", guard=self.name,
                               reason="回复含敏感个人信息，已脱敏", text=masked)
        return GuardResult(action="pass", guard=self.name)


class ContactInfoGuard:
    name = "contact_info"

    def check(self, text: str) -> GuardResult:
        original = text or ""
        if any(p.search(original) for p in _CONTACT_COMPILED):
            return GuardResult(
                action="sanitize", guard=self.name,
                reason="回复含引导站外联系/交易内容，已替换为平台提醒",
                text=_CONTACT_REMINDER,
            )
        return GuardResult(action="pass", guard=self.name)
