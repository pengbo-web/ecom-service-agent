"""交接上下文包构建。"""

from typing import Optional


def build_handoff_bundle(session_id: str, user_input: str, reply: str,
                         intent: str, confidence: float, reasons: list,
                         recent_context: Optional[list] = None) -> dict:
    return {
        "session_id": session_id,
        "user_input": user_input,
        "reply": reply,
        "intent": intent,
        "confidence": confidence,
        "reasons": reasons,
        "recent_context": recent_context or [],
    }
