"""FastAPI 应用工厂：SSE 流式对话 + 会话重置 + 静态前端 + 可观测性看板。"""

import json
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from app.api.schemas import ChatRequest, ResetRequest
from app.api.session_manager import SessionManager
from app.api.streaming import run_agent_streaming
from app.config.settings import settings
from app.guardrails.pipeline import build_default_pipeline
from app.hardening.auth import make_admin_auth
from app.hardening.cost_guard import CostGuard
from app.hardening.fast_path import match_fast_path
from app.hardening.rate_limit import RateLimiter
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
               hitl: Optional[HitlManager] = None,
               admin_token: Optional[str] = None) -> FastAPI:
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

    # 生产加固（W3.5）
    rate_limiter = RateLimiter(settings.rate_limit_per_min)
    cost_guard = CostGuard(settings.daily_request_budget)
    _admin_token = admin_token if admin_token is not None else settings.admin_token
    admin_auth = make_admin_auth(_admin_token)

    def _reply_stream(text: str):
        def gen():
            yield _sse_frame({"type": "reply", "content": text})
            yield _sse_frame({"type": "done"})
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/chat")
    def chat(req: ChatRequest):
        # 1) 限流（防刷）
        if not rate_limiter.allow(req.session_id):
            return _reply_stream("⏳ 您发送得太快啦，请稍后再试～")

        # 2) 人工接管中：短路，不调用 Agent
        if hitl is not None and hitl.manual_mode.is_manual(req.session_id):
            return _reply_stream("🎧 当前会话已转由人工客服处理，请稍候…")

        # 3) 规则快路径：高频简单意图秒回，跳过 Agent（省 LLM 成本）
        if settings.fast_path_enabled:
            fp = match_fast_path(req.message)
            if fp:
                return _reply_stream(fp["reply"])

        # 4) 成本上限（防烧爆 API Key）
        if not cost_guard.allow():
            return _reply_stream("🛑 今日服务已达使用上限，请明天再来～")

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

    @app.post("/api/session/reset", dependencies=[Depends(admin_auth)])
    def reset(req: ResetRequest):
        manager.reset(req.session_id)
        return {"status": "reset"}

    @app.get("/api/metrics", dependencies=[Depends(admin_auth)])
    def metrics():
        return compute_metrics(store)

    @app.get("/api/traces", dependencies=[Depends(admin_auth)])
    def traces(limit: int = 20, session_id: Optional[str] = None):
        return store.recent_traces(limit=limit, session_id=session_id)

    @app.get("/api/traces/{trace_id}", dependencies=[Depends(admin_auth)])
    def trace_detail(trace_id: str):
        t = store.get_trace(trace_id)
        return t or {"error": "not found"}

    @app.get("/api/handoffs", dependencies=[Depends(admin_auth)])
    def handoffs():
        return hitl.queue.list_pending() if hitl else []

    @app.post("/api/handoffs/{handoff_id}/resolve", dependencies=[Depends(admin_auth)])
    def resolve_handoff(handoff_id: str):
        ok = hitl.queue.resolve(handoff_id) if hitl else False
        return {"status": "resolved" if ok else "not_found"}

    @app.post("/api/session/{session_id}/takeover", dependencies=[Depends(admin_auth)])
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
