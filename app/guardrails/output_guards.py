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
    # E1:变换类——命中时用 .sub() 就地替换匹配到的号码片段,是"改写"而不是
    # "拒绝/记录"。哪怕只改了文本里一小段,也已经是"买家会看到跟护栏跑之前
    # 不一样的字"，所以跟 ContactInfoGuard 一样必须在流式吐字前就排除掉
    # (见 app/guardrails/base.py OutputGuard 的分类说明)。
    REWRITES_OUTPUT = True

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
    # E1:变换类，而且是这三个护栏里最危险的一种——命中时不是局部脱敏，
    # 是**整段回复替换**成 _CONTACT_REMINDER。哪怕已经流式吐出去的前缀
    # 看起来"干净"，只要后面任何一小段文字命中了引导站外联系的模式，
    # 全文就要被推翻重来——已经出屏的字没有办法收回。这正是"决定是否
    # 流式必须在生成开始前拍板，不能等边生成边判"的第一手证据：任何
    # "先流一部分、发现命中再退化"的增量方案在这个护栏面前都不成立。
    REWRITES_OUTPUT = True

    def check(self, text: str) -> GuardResult:
        original = text or ""
        if any(p.search(original) for p in _CONTACT_COMPILED):
            return GuardResult(
                action="sanitize", guard=self.name,
                reason="回复含引导站外联系/交易内容，已替换为平台提醒",
                text=_CONTACT_REMINDER,
            )
        return GuardResult(action="pass", guard=self.name)
