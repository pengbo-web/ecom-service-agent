"""FastAPI 应用工厂：SSE 流式对话 + 会话重置 + 静态前端 + 可观测性看板。"""

import json
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.api.schemas import ChatRequest, ResetRequest
from app.api.session_manager import SessionManager
from app.api.streaming import run_agent_streaming
from app.config.settings import settings
from app.guardrails.pipeline import build_default_pipeline
from app.hardening.auth import make_admin_auth
from app.hardening.cost_guard import CostGuard
from app.hardening.fast_path import match_fast_path
from app.hardening.rate_limit import RateLimiter
from app.session.lock import get_session_lock
from app.evaluation.regression import load_baseline
from app.evaluation.runner import EvalRunner
from app.evaluation.run_service import run_evaluation
from app.evaluation.trace_to_case import collect_reflow_cases
from app.hitl.manager import HitlManager
from app.hitl.manual_mode import ManualMode
from app.hitl.queue import HandoffQueue
from app.observability import TraceStore, Tracer
from app.observability.metrics import compute_metrics

_ROOT = Path(__file__).resolve().parents[2]

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"
_DIST_DIR = _WEB_DIR / "dist"


def _sse_frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(session_manager: Optional[SessionManager] = None,
               trace_store: Optional[TraceStore] = None,
               hitl: Optional[HitlManager] = None,
               admin_token: Optional[str] = None,
               eval_runner: Optional[EvalRunner] = None) -> FastAPI:
    app = FastAPI(title="Ecom Service Agent API")
    if session_manager is not None:
        manager = session_manager
    else:
        from app.session.archive import build_archiver
        manager = SessionManager(archiver=build_archiver(settings.archive_enabled))

    # 生产路径(未注入 manager)才启动空闲回收线程:空闲超时自动巩固长期记忆。
    # 测试都会注入 session_manager,因此不会误起后台线程。
    if session_manager is None and settings.auto_consolidate_enabled:
        manager.start_reaper(settings.reaper_interval, settings.session_idle_ttl)

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
    session_lock = get_session_lock()   # R4:会话级并发锁(单实例本地/多实例 Redis)
    rate_limiter = RateLimiter(settings.rate_limit_per_min)
    cost_guard = CostGuard(settings.daily_request_budget)
    _admin_token = admin_token if admin_token is not None else settings.admin_token
    admin_auth = make_admin_auth(_admin_token)

    _baseline_path = _ROOT / settings.eval_baseline_path
    _mode = "multi" if settings.multi_agent_enabled else "single"
    if eval_runner is None:
        eval_runner = EvalRunner(
            eval_fn=lambda: run_evaluation(mode=_mode, use_judge=False),
            baseline_path=_baseline_path,
            tolerance=settings.eval_regression_tolerance,
        )

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
        #    仍把这轮问答写进会话历史并落盘，保证刷新/切换后可回显（不因走快路径而丢失）。
        if settings.fast_path_enabled:
            fp = match_fast_path(req.message)
            if fp:
                agent = manager.get_or_create(req.session_id, req.user_id)
                with session_lock.guard(req.session_id) as got:
                    if got:
                        msgs = getattr(agent, "raw_messages", None)
                        if isinstance(msgs, list):
                            msgs.append({"role": "user", "content": req.message})
                            msgs.append({"role": "assistant", "content": json.dumps({
                                "intent": fp.get("intent", "fast_path"), "confidence": 1.0,
                                "reply": fp["reply"], "requires_human": False,
                                "follow_up_question": None,
                            }, ensure_ascii=False)})
                            save = getattr(agent, "save", None)
                            if callable(save):
                                try:
                                    save()
                                except Exception:  # noqa: BLE001 保存失败不影响本轮回复
                                    pass
                return _reply_stream(fp["reply"])

        # 4) 成本上限（防烧爆 API Key）
        if not cost_guard.allow():
            return _reply_stream("🛑 今日服务已达使用上限，请明天再来～")

        agent = manager.get_or_create(req.session_id, req.user_id)

        def event_stream():
            # 会话级并发锁:同一会话串行(单实例进程内锁/多实例 Redis 分布式锁)
            with session_lock.guard(req.session_id) as got:
                if not got:
                    yield _sse_frame({"type": "reply",
                                      "content": "⏳ 您的上一条消息还在处理中，请稍候再发～"})
                    yield _sse_frame({"type": "done"})
                    return
                for event in run_agent_streaming(
                    agent, req.message, tracer=tracer,
                    session_id=req.session_id, guard_pipeline=guard_pipeline,
                    hitl=hitl, confirm=req.confirm,
                ):
                    yield _sse_frame(event)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/session/reset", dependencies=[Depends(admin_auth)])
    def reset(req: ResetRequest):
        manager.reset(req.session_id)
        return {"status": "reset"}

    @app.get("/api/session/{session_id}/history")
    def session_history(session_id: str):
        """回显该会话已落盘的历史气泡(重启/刷新后聊天记录不再空白)。与 /api/chat 同等公开。"""
        from app.api.history import reconstruct_bubbles
        return {"session_id": session_id, "turns": reconstruct_bubbles(manager.peek_messages(session_id))}

    @app.post("/api/session/{session_id}/consolidate", dependencies=[Depends(admin_auth)])
    def consolidate(session_id: str, user_id: str = "default"):
        """把本会话对话巩固进长期记忆(触发 Phase 5 策展),并回传当前长期记忆事实。

        生产环境由空闲超时自动巩固(见 SessionManager.sweep/start_reaper);
        此端点是运维/演示用的手动触发,便于即时观察策展效果而不必等空闲 TTL。
        """
        agent = manager.get_or_create(session_id, user_id)
        mm = getattr(agent, "memory_manager", None)
        if mm is None or not getattr(mm, "memory_enabled", False):
            return {"enabled": False, "count": 0, "facts": []}
        with manager.get_lock(session_id):
            mm.consolidate_to_long_term(
                getattr(agent, "raw_messages", []), getattr(agent, "summary", None),
            )
            facts = [
                {"content": f.content, "category": f.category, "created_at": f.created_at}
                for f in mm.ltm.facts
            ]
        return {"enabled": True, "curation": settings.memory_curation_enabled,
                "count": len(facts), "facts": facts}

    @app.post("/api/config/reload", dependencies=[Depends(admin_auth)])
    def config_reload():
        """热更新:重读 .env,把变化的行为字段应用到在运行的限流/成本/HITL 对象,免重启。"""
        from app.config.hot_reload import reload_settings
        changed = reload_settings()
        applied = []
        if "rate_limit_per_min" in changed:
            rate_limiter.max = settings.rate_limit_per_min
            applied.append("rate_limit_per_min")
        if "daily_request_budget" in changed:
            cost_guard.max = settings.daily_request_budget
            applied.append("daily_request_budget")
        if "hitl_confidence_threshold" in changed and hitl is not None:
            hitl.confidence_threshold = settings.hitl_confidence_threshold
            applied.append("hitl_confidence_threshold")
        model_changed = [f for f in ("model_name", "fallback_model", "fallback_base_url") if f in changed]
        note = "模型变更对新建会话即时生效(现有会话下次重建后生效)。" if model_changed else ""
        return {"changed": changed, "applied": applied, "model_changed": model_changed, "note": note}

    @app.get("/api/memory", dependencies=[Depends(admin_auth)])
    def memory(user_id: str = ""):
        """只读:从磁盘加载指定用户的长期记忆(反映真实存储,不触发巩固)。"""
        from app.agent.memory.long_term import LongTermMemory
        uid = user_id or settings.memory_user_id
        ltm = LongTermMemory(
            user_id=uid,
            memory_dir=settings.memory_dir,
            max_facts=settings.max_ltm_facts,
        )
        ltm.load()
        return {
            "user_id": uid,
            "curation": settings.memory_curation_enabled,
            "count": len(ltm.facts),
            "facts": [
                {"content": f.content, "category": f.category,
                 "created_at": f.created_at, "source_session": f.source_session}
                for f in ltm.facts
            ],
            "interaction_summaries": ltm.interaction_summaries[-10:],
        }

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

    @app.post("/api/reflow", dependencies=[Depends(admin_auth)])
    def reflow(limit: int = 200):
        cases = collect_reflow_cases(store, limit=limit) if store else []
        return {"count": len(cases), "cases": cases}

    @app.get("/api/eval/baseline", dependencies=[Depends(admin_auth)])
    def eval_baseline():
        return load_baseline(eval_runner.baseline_path) or {}

    @app.post("/api/eval/run", dependencies=[Depends(admin_auth)])
    def eval_run():
        return eval_runner.start()

    @app.get("/api/eval/status", dependencies=[Depends(admin_auth)])
    def eval_status():
        return eval_runner.status()

    # 单页 SPA（web/dist）；/ 与 /dashboard 都进这个应用（前端按路径定位到看板 Tab）
    _spa_index = _DIST_DIR / "index.html"
    if _spa_index.exists():
        app.mount("/assets", StaticFiles(directory=str(_DIST_DIR / "assets")), name="assets")

    def _serve_spa() -> HTMLResponse:
        if _spa_index.exists():
            # 入口 HTML 不缓存,确保重新构建后浏览器立刻拿到新包(哈希化的 assets 仍可长缓存)
            return HTMLResponse(_spa_index.read_text(encoding="utf-8"),
                                headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        return HTMLResponse(
            "<h1>前端未构建</h1><p>请先执行：cd webui &amp;&amp; npm install &amp;&amp; npm run build</p>",
            status_code=503,
        )

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _serve_spa()

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return _serve_spa()

    return app
