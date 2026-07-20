"""输入侧护栏：Prompt Injection / 越狱检测（规则层）。"""

import re

from app.guardrails.base import GuardResult

# 规则层：命中任一即判定为注入/越狱尝试。大小写不敏感。
_PATTERNS = [
    r"忽略(以上|之前|前面|上述|所有).{0,6}(指令|指示|要求|规则|设定|提示)",
    r"ignore\s+(the\s+)?(previous|above|prior|all)\b.{0,20}instruction",
    r"(开发者模式|developer\s*mode|越狱|jailbreak|\bDAN\b)",
    r"(系统提示|系统提示词|system\s*prompt).{0,10}(是什么|告诉我|输出|泄露|重复|reveal|repeat)",
    r"(从现在起|从此以后|现在开始).{0,8}你(是|扮演|将成为)",
    r"(重复|复述|打印).{0,6}(上面|以上|你的|系统).{0,6}(内容|指令|提示)",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in _PATTERNS]


class PromptInjectionGuard:
    name = "prompt_injection"

    def check(self, text: str) -> GuardResult:
        for pat in _COMPILED:
            if pat.search(text or ""):
                return GuardResult(
                    action="block", guard=self.name,
                    reason=f"疑似 Prompt Injection / 越狱指令（命中: {pat.pattern[:24]}…）",
                )
        return GuardResult(action="pass", guard=self.name)
