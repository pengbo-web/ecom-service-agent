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


def build_default_pipeline() -> GuardPipeline:
    return GuardPipeline(
        input_guards=[PromptInjectionGuard()],
        output_guards=[SensitiveInfoGuard(), ContactInfoGuard()],
    )
