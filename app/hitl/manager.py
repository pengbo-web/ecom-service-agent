"""HITL 编排：升级判定 + 入队 + 人工模式。"""

from app.hitl.escalation import should_escalate, SENSITIVE_INTENTS
from app.hitl.handoff import build_handoff_bundle


class HitlManager:
    def __init__(self, queue, manual_mode, confidence_threshold: float,
                 sensitive_intents: set = SENSITIVE_INTENTS):
        self.queue = queue
        self.manual_mode = manual_mode
        self.confidence_threshold = confidence_threshold
        self.sensitive_intents = sensitive_intents

    def evaluate(self, intent: str, confidence: float, requires_human: bool,
                 user_input: str = "") -> list:
        return should_escalate(intent, confidence, requires_human,
                               self.confidence_threshold, self.sensitive_intents,
                               user_input=user_input)

    def escalate(self, session_id, user_input, reply, intent, confidence,
                 reasons, recent_context=None) -> str:
        bundle = build_handoff_bundle(session_id, user_input, reply, intent,
                                      confidence, reasons, recent_context)
        return self.queue.add(bundle)
