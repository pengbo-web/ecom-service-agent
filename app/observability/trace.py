"""Trace / Span 数据模型。"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Span:
    span_id: str
    trace_id: str
    name: str
    kind: str            # "llm" | "tool"
    started_at: float
    ended_at: float
    latency_ms: float
    success: Optional[bool] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    meta: dict = field(default_factory=dict)


@dataclass
class Trace:
    trace_id: str
    session_id: str
    user_input: str
    intent: Optional[str]
    started_at: float
    ended_at: float
    latency_ms: float
    status: str          # "ok" | "error"
    error: Optional[str]
    spans: list = field(default_factory=list)

    @property
    def prompt_tokens(self) -> int:
        return sum(s.prompt_tokens for s in self.spans)

    @property
    def completion_tokens(self) -> int:
        return sum(s.completion_tokens for s in self.spans)
