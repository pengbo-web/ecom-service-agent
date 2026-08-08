"""护栏管道：编排输入/输出护栏。"""

from app.guardrails.base import GuardResult
from app.guardrails.input_guards import PromptInjectionGuard
from app.guardrails.output_guards import SensitiveInfoGuard, ContactInfoGuard


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
        """E1(回复流式化)用:这一条 pipeline 的输出护栏里，是否存在"变换类"
        (`OutputGuard.REWRITES_OUTPUT = True`)——只要有一个，这一轮就不能
        逐块吐字给买家（原因见 app/guardrails/base.py 的分类说明）。

        这里判的是护栏的**类型**，不是"这段具体文本会不会真的触发"——后者
        要等文本生成完才知道，而流式与否必须在生成开始前就决定（否则已经
        流出去的前缀在护栏真的命中时收不回来，ContactInfoGuard 的整段替换
        就是铁证）。宁可放弃一些"其实不会命中"的流式机会，也不换取任何
        "命中了但已经吐出去"的可能。"""
        return any(getattr(g, "REWRITES_OUTPUT", False) for g in self.output_guards)


def build_default_pipeline() -> GuardPipeline:
    return GuardPipeline(
        input_guards=[PromptInjectionGuard()],
        output_guards=[SensitiveInfoGuard(), ContactInfoGuard()],
    )
