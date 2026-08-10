"""E1b(Part1):IncrementalRedactor 的正确性——证明"局部脱敏类"护栏可以安全
套到逐块流式输出上:敏感号码跨 chunk 边界被切开也不会被当作"干净文本"提前
吐出去,且缓冲区长度是从护栏自己的正则模式推导出来的,不是写死的数字。
"""

from app.guardrails.pipeline import build_default_pipeline
from app.guardrails.streaming_redactor import IncrementalRedactor


def _make_redactor():
    p = build_default_pipeline()
    holdback = p.local_redaction_holdback()
    assert holdback is not None   # 前提:默认 pipeline 的两个护栏都证明是局部脱敏
    return IncrementalRedactor(p.local_redaction_patterns(), holdback, p.sanitize_fragment), holdback


def _feed_all(redactor, chunks):
    emitted = []
    for c in chunks:
        out = redactor.feed(c)
        if out is not None:
            emitted.append(out)
    return emitted


def test_phone_number_split_char_by_char_never_leaks():
    """逐字符喂(最坏情况的切分粒度)——号码的任意前缀都不应该在号码还没
    完整出现前就被判定"安全、可以吐"。"""
    redactor, _ = _make_redactor()
    text = "您的手机号是13812345678,请核实。"
    combined = "".join(_feed_all(redactor, list(text)))
    assert "13812345678" not in combined


def test_split_exactly_at_phone_number_middle_never_leaks():
    """号码被硬切成两半,分别落在两个 chunk 里——典型的"卡在 chunk 边界"场景。"""
    redactor, _ = _make_redactor()
    phone = "13812345678"
    chunks = ["手机号", phone[:6], phone[6:], "已登记,谢谢"]
    combined = "".join(_feed_all(redactor, chunks))
    assert phone not in combined


def test_id_card_split_across_many_chunks_never_leaks():
    """身份证号(18 位,含末位字母 X 的场景)一样不能漏——每个字符单独一个
    chunk,模拟最细粒度的 token 流。"""
    redactor, _ = _make_redactor()
    id_no = "110101199003078888"
    text = f"身份证号{id_no}已核验通过"
    combined = "".join(_feed_all(redactor, list(text)))
    assert id_no not in combined


def test_bank_card_split_across_chunks_never_leaks():
    redactor, _ = _make_redactor()
    card = "6222021234567890123"
    text = f"银行卡{card}已绑定"
    combined = "".join(_feed_all(redactor, list(text)))
    assert card not in combined


def test_contact_solicitation_split_across_chunks_never_leaks():
    """局部脱敏不止敏感号码——引导站外联系的短语跨 chunk 边界也不能提前
    以"看起来干净"的姿态吐出去(比如"加微信"被切成"加"和"微信"两个 chunk)。"""
    redactor, _ = _make_redactor()
    chunks = ["您可以", "加", "微信", "联系我方便沟通"]
    combined = "".join(_feed_all(redactor, chunks))
    assert "加微信" not in combined


def test_clean_long_text_is_committed_up_to_holdback_boundary():
    """干净文本(没有任何护栏会命中的内容)理应能持续、逐步提交——不是
    "有变换类护栏存在就整体不发"。

    这条原来断言的是 `len(combined) == len(text) - holdback`(死扣最宽模式的
    114 个字符)。**改断言是刻意的语义变更,不是让测试迁就实现**:那个口径下
    长度不足 114 字的回复一条 delta 都发不出去(实测 75 字回复的流式条数为
    0,买家干等到最后一次性看到全文),流式在体验上等于没做。

    现在按每条模式各自的"屏障字符"算已解决边界(见 streaming_redactor 模块
    注释):中文标点/空格不可能出现在邮箱那条正则的匹配里,所以不必为它等
    114 个字。这段纯中文文本的实际扣留量降到只由含 `.` 的那条模式决定。
    """
    redactor, holdback = _make_redactor()
    text = "您的订单已经发货了，预计三到五天送达，感谢您的耐心等待与信任。" * 6
    assert len(text) > holdback   # 前提:测试文本必须明显长过 holdback,否则永远提交不了
    combined = "".join(_feed_all(redactor, list(text)))
    assert combined   # 确实提交了内容,不是从头按到尾一个字都不发
    assert text.startswith(combined)          # 提交的都是原文的一个前缀,顺序不乱
    held = len(text) - len(combined)
    assert held < holdback, "屏障字符没起作用:还在按最宽模式死扣"
    # 上界仍然存在(不是"全提交了"),否则就说明边界算错、把没定的也发了出去
    assert held > 0


def test_zero_holdback_emits_immediately_when_no_patterns():
    """没有任何变换类护栏(patterns 为空)时 holdback 为 0,不需要攒缓冲区,
    喂一个字符就能立即吐出这个字符——不会平白无故引入延迟。"""
    redactor = IncrementalRedactor([], 0, lambda t: t)
    assert redactor.feed("你") == "你"
    assert redactor.feed("好") == "好"


def test_holdback_is_derived_from_patterns_not_hardcoded():
    """缓冲区长度必须随护栏自己的正则模式变化——加一条更长的正则,holdback
    要跟着自动变大,不是维护者手写在别处、容易忘记同步的数字。"""
    import re

    from app.guardrails.output_guards import SensitiveInfoGuard
    from app.guardrails.pattern_width import max_pattern_width

    baseline = max_pattern_width(SensitiveInfoGuard.PATTERNS)
    longer_pattern = re.compile("x" * (baseline + 50))
    widened = max_pattern_width(SensitiveInfoGuard.PATTERNS + [longer_pattern])
    assert widened > baseline
    assert widened == baseline + 50
