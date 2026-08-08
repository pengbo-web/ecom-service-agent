r"""E1b(流式安全脱敏):静态计算一组正则「最长可能匹配宽度」，供增量脱敏
determinam 缓冲区长度用——不是拍脑袋的常数,是从模式本身推导出来的。

为什么需要这个:SensitiveInfoGuard/ContactInfoGuard 这类"局部替换匹配片段"
的护栏，理论上可以安全地套到逐块流式输出上——只要吐字前"扣住"足够长的尾部
不发，保证任何还可能跨 chunk 边界继续增长的匹配都不会被过早地当作"安全"
提前吐出去。这个"足够长"到底多长，必须跟着护栏自己的正则模式走：模式改了
(比如身份证号规则从18位改成新格式)，缓冲区要跟着自动变宽，不能是维护者手
写在别处、容易忘记同步的魔数。

原理:用 Python `re` 内部的正则解析树（`re._parser`）递归算出"这个模式最多
能吞掉多少个字符"——字面量/字符类/任意字符各占 1 个宽度；零宽断言
(^/$/\b/环视)占 0；有界重复(如 `\d{4}`、`.{0,4}`)= 重复上限 × 子模式宽度；
分支(`a|b|c`)取各分支宽度的最大值；子分组递归求和。

安全前提(必须显式失败,不能悄悄猜一个数):
  - 模式里出现无界重复(`*`、`+`、不带上限的 `{n,}`)——理论最长匹配无穷大，
    没法给出任何有限缓冲区保证安全——直接抛 `UnboundedPatternError`。
  - 出现本模块未识别的正则算子(如反向引用 `\1`，宽度依赖运行时匹配内容，
    没法静态算)——同样抛 `UnboundedPatternError`，宁可让调用方整体退化为
    "不能流式局部脱敏"，也不要用一个可能算少了的数字换取"看起来能流式"的
    假安全感。

调用方(见 app/guardrails/pipeline.py)据此判定:某个变换类输出护栏如果暴露
了 `PATTERNS`（编译后的正则列表）且这里能算出有限宽度，才认定它"可以安全
局部脱敏地流式"；算不出来 = 老路径(生成完→护栏跑完→一次性发)，不会因为
新增了一个宽度算不出来的模式而悄悄破坏"不泄漏"的保证。
"""

import re
from re import _parser as _re_parser  # 私有 API，但是 stdlib 里唯一能拿到解析树的入口
from typing import Iterable


class UnboundedPatternError(ValueError):
    """模式包含无界重复或本模块未识别的算子——静态宽度不可计算。"""


def _seq_width(seq) -> int:
    return sum(_node_width(op, av) for op, av in seq)


def _node_width(op, av) -> int:
    if op in (_re_parser.LITERAL, _re_parser.NOT_LITERAL, _re_parser.ANY,
              _re_parser.IN, _re_parser.CATEGORY):
        return 1   # 单字符类:字面量/否定字面量/任意字符/字符集([...]、\d 等)
    if op == _re_parser.AT:
        return 0   # 零宽锚点:^、$、\b、\B
    if op in (_re_parser.ASSERT, _re_parser.ASSERT_NOT):
        return 0   # 环视断言(?=...)/(?!...)/(?<=...)/(?<!...):不消耗字符
    if op == _re_parser.SUBPATTERN:
        # av = (group, add_flags, del_flags, subpattern)
        return _seq_width(av[3])
    if op in (_re_parser.MAX_REPEAT, _re_parser.MIN_REPEAT):
        lo, hi, sub = av
        if hi == _re_parser.MAXREPEAT:
            raise UnboundedPatternError(
                f"模式包含无界重复(重复上限={hi!r})，无法静态计算最长匹配宽度"
            )
        return hi * _seq_width(sub)
    if op == _re_parser.BRANCH:
        # av = (None, [branch1, branch2, ...])
        branches = av[1]
        if not branches:
            return 0
        return max(_seq_width(b) for b in branches)
    # 反向引用(\1)、条件分组等:宽度依赖运行时匹配到的具体内容,静态算不出来；
    # 本仓库现有护栏模式都不含这些算子——真出现就是维护者引入了新写法，必须
    # 显式失败,不能悄悄假设一个宽度。
    raise UnboundedPatternError(f"未识别的正则算子 {op!r}，无法静态计算最长匹配宽度")


def pattern_max_width(pattern: "re.Pattern") -> int:
    """单个编译后正则的最长可能匹配宽度(字符数)。模式含无界重复/未识别算子
    时抛 UnboundedPatternError。"""
    parsed = _re_parser.parse(pattern.pattern, flags=pattern.flags)
    return _seq_width(parsed)


def max_pattern_width(patterns: Iterable["re.Pattern"]) -> int:
    """一组正则里最长的那个的最长可能匹配宽度；空列表返回 0。"""
    widths = [pattern_max_width(p) for p in patterns]
    return max(widths, default=0)
