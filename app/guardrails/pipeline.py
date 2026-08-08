"""护栏管道：编排输入/输出护栏。"""

from typing import Optional

from app.guardrails.base import GuardResult
from app.guardrails.input_guards import PromptInjectionGuard
from app.guardrails.output_guards import SensitiveInfoGuard, ContactInfoGuard
from app.guardrails.pattern_width import max_pattern_width, UnboundedPatternError


class GuardPipeline:
    def __init__(self, input_guards: list, output_guards: list):
        self.input_guards = input_guards
        self.output_guards = output_guards

    def check_input(self, text: str) -> GuardResult:
        for g in self.input_guards:
            r = g.check(text)
            if r.action == "block":
                return r
        return GuardResult(action="pass")

    def check_output(self, text: str):
        results = []
        current = text
        for g in self.output_guards:
            r = g.check(current)
            if r.action != "pass":
                results.append(r)
                if r.text is not None:
                    current = r.text
        return current, results

    def has_rewriting_output_guard(self) -> bool:
        """这一条 pipeline 的输出护栏里，是否存在"变换类"
        (`OutputGuard.REWRITES_OUTPUT = True`)——判的是护栏的**类型**，不是
        "这段具体文本会不会真的触发"（后者要等文本生成完才知道）。

        E1b 之前，这个方法直接等价于"这一轮不能逐块吐字"；E1b 之后区分开了：
        变换类护栏不再等于不能流式——如果它的改写证明是"局部脱敏"（见
        `local_redaction_holdback`），逐块吐字仍然安全，只是要经过
        `IncrementalRedactor` 顶一层缓冲。这个方法保留下来是给"完全不含任何
        变换类护栏"这种最宽松场景一个快速判断（片段级/增量场景不需要缓冲），
        以及给旧测试/旧调用方一个兼容接口——真正决定"能不能流式"的是
        `local_redaction_holdback() is not None`，不是这个方法。"""
        return any(getattr(g, "REWRITES_OUTPUT", False) for g in self.output_guards)

    def _rewriting_output_guards(self) -> list:
        return [g for g in self.output_guards if getattr(g, "REWRITES_OUTPUT", False)]

    def local_redaction_holdback(self) -> Optional[int]:
        """E1b(流式安全脱敏)核心判定:这条 pipeline 的输出护栏能否安全套到
        逐块流式输出上，以及需要预留多长的尾部缓冲区(字符数)。

        返回:
          - ``None``：存在至少一个变换类护栏"不能证明是局部脱敏"——它没有
            暴露 ``PATTERNS``（不知道它改写的范围有多大，比如可能整段替换），
            或者暴露了但里面有无界模式（静态算不出最长匹配宽度，见
            ``pattern_width.UnboundedPatternError``）。这两种情况都必须
            fail-closed：整轮退化为老路径（生成完 → 护栏跑完 → 一次性发），
            因为"已经流出去的前缀事后收不回来"这条约束一旦可能被违反，就
            不能赌它不会发生。
          - ``int``（可能是 0）：pipeline 里所有变换类护栏都证明是局部脱敏，
            这个数字是所有这些护栏的正则里最长可能匹配宽度的最大值——喂给
            ``IncrementalRedactor`` 当尾部缓冲区长度用。0 表示没有任何变换类
            护栏（不需要缓冲，从第一个字符开始就能安全流）。

        这里只看"护栏类型能否证明安全"，不看"这段具体文本会不会真的命中"——
        跟 `has_rewriting_output_guard` 一样是类型级判断，只是判断的问题从
        "有没有变换类护栏"细化成了"变换类护栏的改写范围有没有界"。
        """
        max_width = 0
        for g in self._rewriting_output_guards():
            patterns = getattr(g, "PATTERNS", None)
            if not patterns:
                return None
            try:
                width = max_pattern_width(patterns)
            except UnboundedPatternError:
                return None
            max_width = max(max_width, width)
        return max_width

    def local_redaction_patterns(self) -> list:
        """所有变换类护栏用于判定"匹配"的编译正则的合集(供
        `IncrementalRedactor` 判断某个 commit 边界是否会把一个真实匹配切
        成两半用)。只在 `local_redaction_holdback() is not None` 时才有意义
        （否则说明存在无法枚举 PATTERNS 的护栏，压根不会走增量流式这条路）。"""
        patterns = []
        for g in self._rewriting_output_guards():
            patterns.extend(getattr(g, "PATTERNS", None) or [])
        return patterns

    def sanitize_fragment(self, text: str) -> str:
        """对一小段文本(增量流式场景下"已确认安全、可以吐给买家"的新增前缀)
        依次跑每个变换类护栏的改写逻辑——语义等价于 `check_output` 对整段
        回复做的替换，只是作用对象从"完整回复"换成"这一小段"。

        只被 `IncrementalRedactor` 使用；`check_output` 仍然是对完整回复文本
        做最终、权威的那一次护栏检查（见 app/api/streaming.py `_finalize`），
        这里的结果只影响买家中途看到的增量分片，不影响终帧内容。"""
        current = text
        for g in self._rewriting_output_guards():
            r = g.check(current)
            if r.action == "sanitize" and r.text is not None:
                current = r.text
        return current


def build_default_pipeline() -> GuardPipeline:
    return GuardPipeline(
        input_guards=[PromptInjectionGuard()],
        output_guards=[SensitiveInfoGuard(), ContactInfoGuard()],
    )
