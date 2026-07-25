"""HITL 编排：升级判定 + 入队 + 人工模式。"""

from app.hitl.escalation import should_escalate, SENSITIVE_INTENTS
from app.hitl.handoff import build_handoff_bundle


class HitlManager:
    def __init__(self, queue=None, manual_mode=None, confidence_threshold: float = 0.6,
                 sensitive_intents: set = SENSITIVE_INTENTS):
        self.queue = queue
        self.manual_mode = manual_mode
        self.confidence_threshold = confidence_threshold
        self.sensitive_intents = sensitive_intents

    def evaluate(self, intent: str, confidence: float, requires_human: bool,
                 user_input: str = "", qu_intent: str = "",
                 prior_user_msgs: list | None = None) -> list:
        from app.config.settings import settings
        return should_escalate(intent, confidence, requires_human,
                               self.confidence_threshold, self.sensitive_intents,
                               user_input=user_input, qu_intent=qu_intent,
                               prior_user_msgs=prior_user_msgs,
                               repeat_times=settings.hitl_repeat_times)

    def escalate(self, session_id, user_input, reply, intent, confidence,
                 reasons, recent_context=None) -> str:
        bundle = build_handoff_bundle(session_id, user_input, reply, intent,
                                      confidence, reasons, recent_context)
        return self.queue.add(bundle)
