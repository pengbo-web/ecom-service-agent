r"""输出侧护栏：敏感信息脱敏 + 联系方式外流拦截。

E1b(流式安全脱敏):两个护栏都会改写文本(``REWRITES_OUTPUT = True``)，但
改写的"作用范围"不同,这是能否安全套到逐块流式输出上的关键:

  - ``SensitiveInfoGuard``:一直是局部替换——``.sub()`` 只动匹配到的那一小
    段号码,匹配范围外的字符原样不动。
  - ``ContactInfoGuard``:E1b 之前是**整段回复替换**成 ``_CONTACT_REMINDER``
    (教训见下方 class docstring——一次误判就让买家看不到任何有用内容)。
    E1b 改成跟 ``SensitiveInfoGuard`` 同款的局部替换:只删掉匹配到的"引导站
    外联系"那几个字,回复其余部分原样保留，末尾追加一条平台提醒(不是拿这条
    提醒去顶替全文)。防护目的不变(违规内容不会完整触达买家)，代价从"整段
    回复陪葬"降到"删掉几个字"。

两个护栏都额外暴露 ``PATTERNS``(参与匹配的编译后正则列表)——这是
``app/guardrails/pipeline.py`` 判定"能否安全增量流式脱敏"、
``app/guardrails/pattern_width.py`` 静态推算"需要预留多长的尾部缓冲区"的
唯一依据；新增/修改任何一条正则，缓冲区都会跟着自动变，不需要在别处手动
同步一个数字。
"""

import re

from app.guardrails.base import GuardResult

_RE_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_RE_ID = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
_RE_BANK = re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)")
# E1b:邮箱本地部分/域名理论上无界(RFC 5321 上限 64/255 字节)，但静态宽度
# 一旦按理论上限算，会把"是否有邮箱"这一件事从不涉及的多数回复都拖累成一个
# 几百字符的流式缓冲区(见 app/guardrails/pattern_width.py 的推导)——真实场景
# 里没人会打出/模型没理由生成本地部分 > 32 字符或域名 > 81 字符的邮箱地址。
# 这里显式收紧到有界模式(仍能匹配 99% 以上真实邮箱，包括所有常见邮箱服务商
# 与自建域名)，换来"可以被静态算出一个不太离谱的缓冲区长度"这个前提——收紧
# 的数字写在正则里，不是缓冲区计算逻辑里的暗数，改这两个数字缓冲区自动跟着变。
_RE_EMAIL = re.compile(r"([\w.+-]{1,3})[\w.+-]{0,29}@([\w-]{1,40}\.[\w.-]{1,40})")

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
    # E1b:改写只发生在匹配到的那一小段范围内(局部脱敏)，且每条正则的最长
    # 匹配宽度都能静态算出来(见 pattern_width.py)——满足"可安全增量流式脱敏"
    # 的两个前提，因此暴露 PATTERNS 供 GuardPipeline 判定/推算缓冲区长度。
    PATTERNS = [_RE_PHONE, _RE_ID, _RE_BANK, _RE_EMAIL]

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
    # E1b(原 E1 报告记的教训,现已解决):这个护栏曾经命中就整段替换回复——
    # 旧教训是一次裸词「微信」的误判把一条完全正确的退款说明整段吃掉，买家
    # 什么都看不到。复盘结论:防的是"引导站外联系的那几个字"，不是"提到微信
    # 这件事本身"，没有理由为了删掉几个字连累回复其余全部内容。现改成跟
    # SensitiveInfoGuard 同款的**局部替换**——只删掉匹配到的引导性短语，回复
    # 其余部分原样保留，末尾追加一条平台提醒(见 _CONTACT_REMINDER)。防护
    # 强度不降:命中的引导内容一样一个字不留地不会触达买家，只是不再需要
    # 拿整条回复的其余内容做陪葬。
    REWRITES_OUTPUT = True
    # E1b:_CONTACT_PATTERNS 全部有界(无 `*`/`+`/不限上限的 `{n,}`)，最长匹配
    # 宽度可静态算出，同 SensitiveInfoGuard 一样满足"可安全增量流式脱敏"。
    PATTERNS = _CONTACT_COMPILED

    def check(self, text: str) -> GuardResult:
        original = text or ""
        masked = original
        hit = False
        for p in _CONTACT_COMPILED:
            masked, n = p.subn("", masked)
            hit = hit or n > 0
        if hit:
            # 局部删除引导短语后,原文其余内容原样保留;末尾追加一次提醒
            # (不管命中几处/几种模式，提醒只加一条，不随命中次数重复)。
            masked = masked.strip()
            final_text = (masked + "\n" + _CONTACT_REMINDER) if masked else _CONTACT_REMINDER
            return GuardResult(
                action="sanitize", guard=self.name,
                reason="回复含引导站外联系/交易内容，已局部移除并追加平台提醒",
                text=final_text,
            )
        return GuardResult(action="pass", guard=self.name)
