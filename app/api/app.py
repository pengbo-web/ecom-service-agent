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
from app.observability import TraceStore, Tracer
from app.observability.metrics import compute_metrics

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def _sse_frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(session_manager: Optional[SessionManager] = None,
               trace_store: Optional[TraceStore] = None) -> FastAPI:
    app = FastAPI(title="Ecom Service Agent API")
    manager = session_manager or SessionManager()

    store = trace_store
    tracer = None
    if settings.obs_enabled or trace_store is not None:
        store = trace_store or TraceStore()
        store.init_schema()
        tracer = Tracer(store)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/chat")
    def chat(req: ChatRequest):
        agent = manager.get_or_create(req.session_id)
        lock = manager.get_lock(req.session_id)

        def event_stream():
            with lock:  # 同一会话串行处理，避免并发踩状态
                for event in run_agent_streaming(
                    agent, req.message, tracer=tracer, session_id=req.session_id
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
    def traces(limit: int = 20):
        return store.recent_traces(limit=limit)

    @app.get("/api/traces/{trace_id}")
    def trace_detail(trace_id: str):
        t = store.get_trace(trace_id)
        return t or {"error": "not found"}

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (_WEB_DIR / "chat.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        html = (_WEB_DIR / "dashboard.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    return app
