"""E1b(Part1):pattern_width 的静态正则宽度推导——IncrementalRedactor 的
安全性完全建立在这个数字算得对的前提上。"""

import re

import pytest

from app.guardrails.pattern_width import (
    max_pattern_width, pattern_max_width, UnboundedPatternError,
)


def test_fixed_digit_run_width():
    assert pattern_max_width(re.compile(r"\d{11}")) == 11


def test_lookaround_is_zero_width():
    """零宽断言((?<!\\d)/(?!\\d))本身不计入宽度,只有真正消耗字符的部分算。"""
    p = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
    assert pattern_max_width(p) == 11


def test_bounded_gap_repeat_width():
    """`.{0,4}` 这种有上限的重复,宽度=上限次数 × 子模式宽度(ANY=1)。"""
    assert pattern_max_width(re.compile(r"微信.{0,4}联系")) == 2 + 4 + 2


def test_branch_takes_max_of_alternatives():
    """分支(a|b|c)宽度取各分支里最长的那个,不是相加。"""
    assert pattern_max_width(re.compile(r"(加|留个|换)微信")) == 2 + 2   # "留个"最长(2),+"微信"(2)


def test_optional_group_bounded():
    assert pattern_max_width(re.compile(r"留个?微信")) == 2 + 2   # "个?"最多消耗 1 个字符


def test_subpattern_sums_children():
    assert pattern_max_width(re.compile(r"(\d{4})\d{8,11}(\d{4})")) == 4 + 11 + 4


def test_unbounded_star_raises():
    with pytest.raises(UnboundedPatternError):
        pattern_max_width(re.compile(r"[\w.+-]*"))


def test_unbounded_plus_raises():
    with pytest.raises(UnboundedPatternError):
        pattern_max_width(re.compile(r"[\w-]+"))


def test_unbounded_open_ended_repeat_raises():
    with pytest.raises(UnboundedPatternError):
        pattern_max_width(re.compile(r"\d{8,}"))


def test_max_pattern_width_takes_max_across_patterns():
    patterns = [re.compile(r"\d{3}"), re.compile(r"\d{11}"), re.compile(r"\d{5}")]
    assert max_pattern_width(patterns) == 11


def test_max_pattern_width_empty_list_is_zero():
    assert max_pattern_width([]) == 0


def test_max_pattern_width_propagates_unbounded_error():
    with pytest.raises(UnboundedPatternError):
        max_pattern_width([re.compile(r"\d{3}"), re.compile(r".*")])
