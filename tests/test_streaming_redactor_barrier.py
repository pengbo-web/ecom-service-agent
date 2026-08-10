"""屏障字符收窄 holdback 之后,「不泄漏」这条硬约束必须逐字节不变。

背景:改造前增量脱敏死扣尾部 `max(所有模式的最长宽度)` = 114 个字符,代价是
**长度不足 114 字的回复一条 delta 都发不出去**——实测 75 字回复的流式条数为
0,买家干等到最后一次性看到全文,流式在体验上等于没做。

现在改成按每条模式各自的字母表算屏障(见 streaming_redactor 模块注释)。这是
收窄等待、不动检测能力,但"我认为它安全"不算证明——这个文件用两类测试压住:

  1. **不变量测试**:随机文本 × 随机切分,断言提交出去的文本里永远找不到任何
     一条护栏模式的匹配。这是"不泄漏"的直接定义,不依赖我对屏障的推理是否正确。
  2. **对照测试**:同一段文本,新边界提交的量必须 ⊇ 旧边界(只会更早发,不会
     更晚),且提交内容始终是原文的前缀(顺序、内容都不许变)。
"""

import random
import re

import pytest

from app.guardrails.pipeline import build_default_pipeline
from app.guardrails.pattern_width import pattern_alphabet, pattern_max_width
from app.guardrails.streaming_redactor import IncrementalRedactor


def _pipeline():
    p = build_default_pipeline()
    assert p.local_redaction_holdback() is not None
    return p


def _run(chunks, patterns=None, holdback=None, sanitize=None):
    p = _pipeline()
    r = IncrementalRedactor(
        patterns if patterns is not None else p.local_redaction_patterns(),
        holdback if holdback is not None else p.local_redaction_holdback(),
        sanitize if sanitize is not None else p.sanitize_fragment,
    )
    out = []
    for c in chunks:
        got = r.feed(c)
        if got is not None:
            out.append(got)
    return "".join(out)


def _chunk(text: str, rng: random.Random) -> list:
    """按随机粒度切分——流式 chunk 边界落在哪里是模型/网络决定的,不可控。"""
    chunks, i = [], 0
    while i < len(text):
        step = rng.randint(1, 7)
        chunks.append(text[i:i + step])
        i += step
    return chunks


# 故意混进各类敏感片段:号码紧贴中文、紧贴标点、被标点夹在中间、跨"屏障字符"。
_SENSITIVE = [
    "13812345678", "110101199003072316", "6222021234567890123",
    "zhangsan@example.com", "a@b.cn", "加微信", "微信号", "私下交易",
    "线下付款", "绕过平台", "QQ", "weixin", "微信详聊",
]
_FILLER = [
    "您的订单已发货", "，", "。", "预计三天送达", " ", "**", "：",
    "ORD-20260728-074", "感谢等待", "（审核中）", "\n", "退款¥5999.00",
]


# 手机号/身份证/银行卡/邮箱——这四类是**掩码型**(SensitiveInfoGuard 用
# `.sub()` 就地替换),所以"明文出现在提交内容里"就是货真价实的泄漏。
#
# 关键词类(QQ/微信/私下交易,ContactInfoGuard)不能用同一把尺子量:它是**删掉
# 引导短语再追加一条平台提醒**,裸词本身在全量脱敏后照样保留(`sanitize_
# fragment("QQ")` 的结果里 `QQ` 还在)。拿"提交内容里不许出现 \\bQQ\\b"当不变量
# 会连全量脱敏这个基准都通不过——那是测试写错了,不是实现漏了。
_MASKING_PAYLOADS = [
    "13812345678", "110101199003072316", "6222021234567890123",
    "zhangsan@example.com", "a@b.cn",
]


@pytest.mark.parametrize("seed", range(40))
def test_never_emits_masked_payload_in_cleartext(seed):
    """不变量:掩码型敏感内容的明文永远不出现在提交给买家的文本里。

    这条不依赖"屏障推导对不对"——它直接检查结论。屏障要是算错了(把还没定
    的内容提前发了),被切开的号码就会以明文出现在 combined 里,这里必挂。
    """
    rng = random.Random(seed)
    parts = []
    for _ in range(rng.randint(4, 14)):
        parts.append(rng.choice(_SENSITIVE) if rng.random() < 0.45 else rng.choice(_FILLER))
    text = "".join(parts)

    combined = _run(_chunk(text, rng))
    # 判据必须**相对于全量脱敏**,不能绝对地"明文不许出现":随机拼接会造出
    # `5999.00` 紧贴卡号这种 21 位连续数字,而 `(?<!\d)…(?!\d)` 对它本来就不
    # 匹配——全量脱敏同样原样保留。拿绝对判据量会把"护栏本来就不管"误判成
    # "增量漏了"。要证的是**增量不比权威的那一遍差**。
    authoritative = _pipeline().sanitize_fragment(text)
    for payload in _MASKING_PAYLOADS:
        if payload in authoritative:
            continue          # 全量脱敏也没掩它:不在本条不变量的射程内
        assert payload not in combined, (
            f"seed={seed} 泄漏了 {payload!r}\n原文={text!r}\n提交={combined!r}")
    from app.guardrails.output_guards import SensitiveInfoGuard
    for pat in SensitiveInfoGuard.PATTERNS:
        if pat.search(authoritative):
            continue
        assert not pat.search(combined), (
            f"seed={seed} 提交内容里还能匹配到 {pat.pattern!r}\n提交={combined!r}")


@pytest.mark.parametrize("seed", range(30))
def test_committed_text_is_always_a_sanitized_prefix(seed):
    """提交内容必须是"原文前缀经过脱敏"的结果:顺序不乱、不重发、不跳字。

    用恒等 sanitize 跑(不改写),这样提交出去的就该是原文的**字面前缀**——
    边界算错会表现为错位或重复,而不只是"多发了点"。
    """
    rng = random.Random(1000 + seed)
    text = "".join(rng.choice(_FILLER) for _ in range(rng.randint(6, 20)))
    p = _pipeline()
    combined = _run(_chunk(text, rng), patterns=p.local_redaction_patterns(),
                    holdback=p.local_redaction_holdback(), sanitize=lambda t: t)
    assert text.startswith(combined), f"seed={seed} 提交内容不是原文前缀"


@pytest.mark.parametrize("seed", range(30))
def test_barrier_commits_at_least_as_much_as_fixed_holdback(seed):
    """对照:新边界提交的量不少于旧的死扣边界——只会更早发,不会更晚。

    对照物必须是**同一组正则**、只关掉屏障——早先我拿 `.{0,N}` 造了个等宽的
    假模式当"旧行为",结果它在缓冲区里处处命中,反而把边界往前推,对照物本身
    就不是旧行为。这里改成直接把各模式的字母表置空(=算不出字母表的退化路
    径),那才是改造前逐字节的口径。
    """
    rng = random.Random(2000 + seed)
    text = "".join(rng.choice(_FILLER + _SENSITIVE) for _ in range(rng.randint(6, 18)))
    p = _pipeline()
    chunks = _chunk(text, rng)

    def run_without_barriers(cs):
        r = IncrementalRedactor(p.local_redaction_patterns(),
                                p.local_redaction_holdback(), lambda t: t)
        for st in r._pstate:
            st["alpha"] = None       # 退化成"死扣各自宽度",即改造前的行为
        out = []
        for c in cs:
            g = r.feed(c)
            if g is not None:
                out.append(g)
        return "".join(out)

    new_len = len(_run(chunks, sanitize=lambda t: t))
    old_len = len(run_without_barriers(chunks))
    assert new_len >= old_len, f"seed={seed} 新边界反而更保守: {new_len} < {old_len}"


def test_universal_alphabet_patterns_fall_back_to_fixed_width():
    """含 `.` 的模式算不出字母表,必须退回死扣宽度——不能因为"算不出来"就
    当成没有约束放开提交。这是本次改造的失败安全侧。"""
    pat = re.compile(r"微信.{0,4}(联系|详聊|私聊)")
    assert pattern_alphabet(pat) is None, "含 `.` 的模式不该给出字母表"
    w = pattern_max_width(pat)
    out = _run(list("请加我微信" + "x" * 3), patterns=[pat], holdback=w, sanitize=lambda t: t)
    text = "请加我微信xxx"
    assert len(out) == len(text) - w   # 逐字节保持改造前的口径


def test_email_barrier_shrinks_wait_on_chinese_text():
    """本次改造要解决的那个具体问题:邮箱那条正则宽 114,而中文标点不可能出
    现在它的匹配里,所以一段带标点的中文回复不该为它等 114 个字。"""
    email_pat = [p for p in _pipeline().local_redaction_patterns() if "@" in p.pattern]
    assert len(email_pat) == 1
    pat = email_pat[0]
    assert pattern_max_width(pat) == 114     # 前提:它就是最宽的那条
    alpha = pattern_alphabet(pat)
    assert alpha is not None
    for ch in "，。：（）* \n":
        assert not alpha(ch), f"{ch!r} 被当成可能出现在邮箱匹配里,屏障就失效了"
    for ch in "aZ0.-+@":
        assert alpha(ch), f"{ch!r} 明明能出现在邮箱里,却被当成屏障——会漏脱敏"

    text = "您的订单已发货，预计三天送达。感谢等待，如有问题随时联系我们。"
    out = _run(list(text), patterns=[pat], holdback=114, sanitize=lambda t: t)
    assert len(out) > len(text) - 114      # 旧口径下这里会是 0
    assert text.startswith(out)


def test_email_split_across_chunks_still_never_leaks():
    """屏障收窄之后,邮箱被切成两半依然不能漏——这是收窄带来的最大风险点,
    单独钉一条:`@` 和字母都在字母表里,不构成屏障,所以必须靠宽度等到底。"""
    email = "zhangsan@example.com"
    combined = _run(["您的邮箱是", email[:6], email[6:12], email[12:], "，已记录。"])
    assert email not in combined
    assert "zhangsan@example.com" not in combined
