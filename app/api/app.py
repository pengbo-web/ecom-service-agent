"""FastAPI 应用工厂：SSE 流式对话 + 会话重置 + 静态前端 + 可观测性看板。"""

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from app.api.schemas import ChatRequest, ResetRequest
from app.api.session_manager import SessionManager
from app.api.streaming import run_agent_streaming
from app.config.settings import settings
from app.guardrails.pipeline import build_default_pipeline
from app.hitl.manager import HitlManager
from app.hitl.manual_mode import ManualMode
from app.hitl.queue import HandoffQueue
from app.observability import TraceStore, Tracer
from app.observability.metrics import compute_metrics

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def _sse_frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(session_manager: Optional[SessionManager] = None,
               trace_store: Optional[TraceStore] = None,
               hitl: Optional[HitlManager] = None) -> FastAPI:
    app = FastAPI(title="Ecom Service Agent API")
    manager = session_manager or SessionManager()

    store = trace_store
    tracer = None
    if settings.obs_enabled or trace_store is not None:
        store = trace_store or TraceStore()
        store.init_schema()
        tracer = Tracer(store)

    guard_pipeline = build_default_pipeline() if settings.guardrails_enabled else None

    if hitl is None and settings.hitl_enabled:
        _hq = HandoffQueue()
        _hq.init_schema()
        hitl = HitlManager(_hq, ManualMode(settings.manual_mode_timeout),
                           settings.hitl_confidence_threshold)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/chat")
    def chat(req: ChatRequest):
        # 人工接管中：短路，不调用 Agent
        if hitl is not None and hitl.manual_mode.is_manual(req.session_id):
            def manual_stream():
                yield _sse_frame({"type": "reply",
                                  "content": "🎧 当前会话已转由人工客服处理，请稍候…"})
                yield _sse_frame({"type": "done"})
            return StreamingResponse(manual_stream(), media_type="text/event-stream")

        agent = manager.get_or_create(req.session_id)
        lock = manager.get_lock(req.session_id)

        def event_stream():
            with lock:  # 同一会话串行处理，避免并发踩状态
                for event in run_agent_streaming(
                    agent, req.message, tracer=tracer,
                    session_id=req.session_id, guard_pipeline=guard_pipeline,
                    hitl=hitl,
                ):
                    yield _sse_frame(event)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/session/reset")
    def reset(req: ResetRequest):
        manager.reset(req.session_id)
        return {"status": "reset"}

    @app.get("/api/metrics")
    def metrics():
        return compute_metrics(store)

    @app.get("/api/traces")
    def traces(limit: int = 20, session_id: Optional[str] = None):
        return store.recent_traces(limit=limit, session_id=session_id)

    @app.get("/api/traces/{trace_id}")
    def trace_detail(trace_id: str):
        t = store.get_trace(trace_id)
        return t or {"error": "not found"}

    @app.get("/api/handoffs")
    def handoffs():
        return hitl.queue.list_pending() if hitl else []

    @app.post("/api/handoffs/{handoff_id}/resolve")
    def resolve_handoff(handoff_id: str):
        ok = hitl.queue.resolve(handoff_id) if hitl else False
        return {"status": "resolved" if ok else "not_found"}

    @app.post("/api/session/{session_id}/takeover")
    def takeover(session_id: str):
        if hitl is None:
            return {"mode": "auto"}
        return {"mode": hitl.manual_mode.toggle(session_id)}

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (_WEB_DIR / "chat.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        # 看板已合并进单页应用；/dashboard 作为别名，前端会自动切到看板标签
        html = (_WEB_DIR / "chat.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    return app
