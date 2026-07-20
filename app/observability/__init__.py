from app.observability.trace import Trace, Span
from app.observability.store import TraceStore
from app.observability.tracer import Tracer

_TRACER = None


def get_tracer():
    return _TRACER


def set_tracer(tracer) -> None:
    global _TRACER
    _TRACER = tracer


__all__ = ["Trace", "Span", "TraceStore", "Tracer", "get_tracer", "set_tracer"]
