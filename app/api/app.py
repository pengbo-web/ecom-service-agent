"""FastAPI 应用工厂：SSE 流式对话 + 会话重置 + 静态前端 + 可观测性看板。"""

import json
import re
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.api.conversations import ensure_active, open_or_reuse
from app.api.schemas import (AgentReplyRequest, ChatRequest, CreateOrderRequest, CreateUserRequest,
                              LoginRequest, OpenConversationRequest, ResetRequest)
from app.api.session_manager import SessionManager
from app.api.streaming import run_agent_streaming
from app.auth.token import sign_token, verify_token
from app.config.settings import settings
from app.db import get_db
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

# /api/admin/skills 的 traces 段查询窗口:按最近轨迹取样,全 skill 共用一个上限
# (高频 skill 可能挤占低频 skill 的样本)。响应里 traces_window.limit 直接引用
# 这个常量,保证"披露的数字"与"实际查询用的数字"不会走偏。
_TRACE_WINDOW = 500


def _sse_frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(session_manager: Optional[SessionManager] = None,
               trace_store: Optional[TraceStore] = None,
               hitl: Optional[HitlManager] = None,
               admin_token: Optional[str] = None,
               eval_runner: Optional[EvalRunner] = None) -> FastAPI:
    # 生产环境安全前置校验:ENVIRONMENT=production 且用默认/空密钥 → 拒绝启动(dev 不校验)
    from app.config.settings import verify_production_secrets
    verify_production_secrets(settings)

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
    _mode = "multi"   # H1.0-C:总控 Agent 唯一入口,恒 multi
    if eval_runner is None:
        eval_runner = EvalRunner(
            eval_fn=lambda: run_evaluation(mode=_mode, use_judge=False),
            baseline_path=_baseline_path,
            tolerance=settings.eval_regression_tolerance,
        )

    def _reply_stream(text: str, conversation: Optional[tuple] = None):
        def gen():
            if conversation is not None:
                cid, rotated = conversation
                yield _sse_frame({"type": "conversation", "conversation_id": cid,
                                  "status": "rotated" if rotated else "active"})
            yield _sse_frame({"type": "reply", "content": text})
            yield _sse_frame({"type": "done"})
        return StreamingResponse(gen(), media_type="text/event-stream")

    # Demo 一键体验:启动即把预置 hmdp 身份写入 Redis(login:token:{demo_token}),
    # 使前端零登录即可以该身份聊真实 hmdp 订单数据。失败静默(Redis 未起时不阻断启动)。
    if settings.demo_mode:
        try:
            from app.api.hmdp_identity import seed_demo_hmdp_identity
            seed_demo_hmdp_identity(settings.demo_hmdp_token, settings.demo_hmdp_user_id,
                                    settings.demo_hmdp_nickname)
            # 第二个 demo 客户(hmdp id 1011,有 1 笔订单),供 ?user=1011 体验并发
            seed_demo_hmdp_identity("demo-hmdp-token-1011", "1011", "另一位顾客")
        except Exception:  # noqa: BLE001
            pass

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/config")
    def public_config():
        """前端启动读取:demo_mode 开时前端自动登录 demo_user_id、跳过登录卡片。"""
        return {"demo_mode": settings.demo_mode,
                "demo_user_id": settings.demo_hmdp_user_id if settings.demo_mode else ""}

    def _map_hmdp_product(p: dict) -> dict:
        imgs = p.get("images")
        img = imgs.split(",")[0] if isinstance(imgs, str) and imgs else (
            imgs[0] if isinstance(imgs, list) and imgs else "")
        return {
            "id": str(p.get("id")), "title": p.get("title"),
            "price": round((p.get("price") or 0) / 100, 2),
            "stock": p.get("stock"), "image": img,
            "description": p.get("description") or "",
        }

    @app.get("/api/products")
    def products(keyword: str = ""):
        """商城:代理 hmdp 商品列表(公开),供前端渲染商品卡。失败返回空列表(降级)。"""
        try:
            import httpx
            base = settings.hmdp_base_url.rstrip("/")
            with httpx.Client(timeout=3.0) as c:
                r = c.get(f"{base}/product/list", params={"keyword": keyword}, timeout=3.0)
                data = r.json() if r.status_code == 200 else {}
            items = (data.get("data") or []) if data.get("success") else []
            return {"products": [_map_hmdp_product(p) for p in items]}
        except Exception:  # noqa: BLE001
            return {"products": []}

    def _fetch_hmdp_product(item_id: str) -> Optional[dict]:
        """按 id 从 hmdp 取单个商品并映射为前端结构;失败/无则 None。"""
        if not item_id.isdigit():
            return None
        try:
            import httpx
            base = settings.hmdp_base_url.rstrip("/")
            with httpx.Client(timeout=3.0) as c:
                r = c.get(f"{base}/product/{item_id}", timeout=3.0)
                data = r.json() if r.status_code == 200 else {}
            p = data.get("data") if data.get("success") else None
            return _map_hmdp_product(p) if p else None
        except Exception:  # noqa: BLE001
            return None

    @app.get("/api/product/{item_id}")
    def product_detail(item_id: str):
        """按 id 取单个商品(结构化),供聊天窗内商品卡渲染。失败/无返回 null。"""
        return {"product": _fetch_hmdp_product(item_id)}

    _ORDER_STATUS_LABELS = {
        "unpaid": "待支付", "pending": "待发货", "shipped": "已发货",
        "delivered": "已签收", "refund_processing": "退款中",
    }

    def _hmdp_token_for_user(user: str) -> str:
        """demo 模式下把登录用户映射到其 hmdp token(与 /api/chat 同一套映射),
        用于让"我的订单"页/自助下单与 AI 读同一份 hmdp 真实订单。非 demo 返回空。"""
        demo_tokens = {str(settings.demo_hmdp_user_id): settings.demo_hmdp_token,
                       "1011": "demo-hmdp-token-1011"}
        return demo_tokens.get(str(user), "") if settings.demo_mode else ""

    def _fmt_order(o: dict) -> dict:
        """统一成"我的订单"页需要的结构(order_id/status/status_label/items/total/…)。"""
        return {
            "order_id": o.get("order_id"),
            "status": o.get("status"),
            "status_label": _ORDER_STATUS_LABELS.get(o.get("status"), o.get("status_text") or o.get("status")),
            "items": o.get("items", []),
            "total": o.get("total"),
            "created_at": (o.get("created_at") or "").replace("T", " "),
            "shipping_address": o.get("shipping_address") or "",
        }

    def _hmdp_my_orders(token: str) -> list[dict]:
        """读 hmdp /order/of/me(与 AI 的 list_user_orders 同源)→ 页面结构。新→旧。"""
        from mcp_server.hmdp_mapping import map_order
        import httpx
        base = settings.hmdp_base_url.rstrip("/")
        with httpx.Client(timeout=4.0) as c:
            r = c.get(f"{base}/order/of/me", headers={"authorization": token}, timeout=4.0)
            d = r.json() if r.status_code == 200 else {}
        raw = (d.get("data") or []) if d.get("success", False) else []
        orders = [_fmt_order(map_order(o)) for o in raw]
        orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)
        return orders

    @app.post("/api/order")
    def create_order(req: CreateOrderRequest, request: Request):
        """自助下单:用户在商城/商品卡点『立即购买』→ 按登录身份建单。
        demo 用户 → 建到 hmdp(与 AI/我的订单页同源,状态待支付);其它 → agent 订单库。"""
        user = _resolve_user(request, None)
        p = _fetch_hmdp_product(req.item_id)
        if not p:
            raise HTTPException(404, "商品不存在或已下架")
        qty = max(1, min(int(req.quantity or 1), 99))
        total = round((p.get("price") or 0) * qty, 2)
        token = _hmdp_token_for_user(user)
        if token:
            import httpx
            base = settings.hmdp_base_url.rstrip("/")
            pid = int(req.item_id) if req.item_id.isdigit() else req.item_id
            with httpx.Client(timeout=4.0) as c:
                r = c.post(f"{base}/order",
                           json={"productId": pid, "quantity": qty,
                                 "address": req.shipping_address or "上海市浦东新区示例路 1 号"},
                           headers={"authorization": token}, timeout=4.0)
                d = r.json() if r.status_code == 200 else {}
            if not d.get("success"):
                raise HTTPException(502, d.get("errorMsg") or "下单失败,请稍后再试")
            return {"success": True, "order_id": d.get("data"), "status_label": "待支付", "total": total}
        order = get_db().create_order(
            user=user,
            items=[{"name": p["title"], "sku": f"HMDP-{req.item_id}", "quantity": qty, "price": p["price"]}],
            total=total, status="pending", shipping_address=req.shipping_address,
        )
        return {"success": True, "order_id": order["order_id"],
                "status_label": _ORDER_STATUS_LABELS.get(order["status"], order["status"]),
                "total": order["total"]}

    @app.get("/api/orders")
    def my_orders(request: Request):
        """当前登录用户的订单列表(供"我的订单"页)。demo 用户读 hmdp 真实订单
        (与 AI list_user_orders 同源,消除"页面/AI 对不齐");其它读 agent 订单库。"""
        user = _resolve_user(request, None)
        token = _hmdp_token_for_user(user)
        if token:
            try:
                return {"orders": _hmdp_my_orders(token)}
            except Exception:  # noqa: BLE001 hmdp 不可用时降级 agent 库
                pass
        raw = [o for o in get_db().list_orders() if o.get("user") == user]
        raw.sort(key=lambda o: o.get("created_at") or "", reverse=True)
        return {"orders": [_fmt_order(o) for o in raw]}

    _UID_RE = re.compile(r"^[\w一-龥-]{1,32}$")

    def _token_user(request: Request):
        """从 Authorization: Bearer 解出 user_id;无/无效返回 None。"""
        auth = request.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        return verify_token(auth[7:], settings.auth_secret)

    def _resolve_user(request: Request, claimed: str | None) -> str:
        """身份解析:门控开=只信 token(缺/坏→401);关=回退自报。"""
        if not settings.auth_enabled:
            return claimed or "default"
        uid = _token_user(request)
        if uid is None:
            raise HTTPException(401, "未登录或登录已过期")
        return uid

    def _issue(user_id: str, name: str):
        return {"user_id": user_id, "name": name,
                "token": sign_token(user_id, settings.auth_secret, settings.auth_token_ttl),
                "expires_in": settings.auth_token_ttl}

    @app.post("/api/users")
    def create_user(req: CreateUserRequest):
        if not _UID_RE.match(req.user_id or ""):
            raise HTTPException(422, "user_id 只允许中英文/数字/下划线/连字符,1-32 位")
        name = (req.name or req.user_id).strip() or req.user_id
        if not get_db().create_user(req.user_id, name):
            raise HTTPException(409, "用户已存在,请直接登录")
        return _issue(req.user_id, name)

    @app.post("/api/auth/login")
    def login(req: LoginRequest):
        u = get_db().get_user(req.user_id)
        if u is None:
            raise HTTPException(404, "用户不存在,请先创建")
        return _issue(u["user_id"], u.get("name") or u["user_id"])

    @app.get("/api/auth/me")
    def me(request: Request):
        uid = _token_user(request)
        if uid is None:
            raise HTTPException(401, "未登录或登录已过期")
        u = get_db().get_user(uid)
        return {"user_id": uid, "name": (u or {}).get("name") or uid}

    @app.post("/api/chat")
    def chat(req: ChatRequest, request: Request):
        # demo 模式:仅当当前登录用户是 demo 客户(或其别名 1011)时,才注入其 hmdp 身份 →
        # 聊真实订单。其它客户(?user=xxx)按自身身份聊,不塌缩成同一 hmdp 身份。
        _demo_tokens = {str(settings.demo_hmdp_user_id): settings.demo_hmdp_token, "1011": "demo-hmdp-token-1011"}
        if settings.demo_mode and not getattr(req, "hmdp_token", "") and str(req.user_id) in _demo_tokens:
            req.hmdp_token = _demo_tokens[str(req.user_id)]
        # 0) 身份解析:优先 hmdp 身份(接 hmdp 数据源时前端传 hmdp_token)——解出即以 hmdp userId
        #    为准(与 tb_order.user_id 同命名空间,MCP 侧凭它做归属);否则回退 agent 自有 token 鉴权。
        _hmdp_uid = None
        if getattr(req, "hmdp_token", ""):
            from app.api.hmdp_identity import resolve_hmdp_user
            _hmdp_uid = resolve_hmdp_user(req.hmdp_token)
        if _hmdp_uid:
            req.user_id = _hmdp_uid
        else:
            req.user_id = _resolve_user(request, req.user_id)

        # 1) 限流（防刷）:auth 开=按已鉴权 user_id(不可伪造);auth 关=按 session_id
        #    (自报 user_id 默认恒为 "default",若按它限流则所有匿名用户共用一个桶、互相拖累;
        #    session_id 至少按会话粒度隔离,更贴近"每客户端限流"的本意)。
        _rl_key = req.user_id if settings.auth_enabled else (req.session_id or req.user_id)
        if not rate_limiter.allow(_rl_key):
            return _reply_stream("⏳ 您发送得太快啦，请稍后再试～")

        # 2) 人工接管中：短路，不调用 Agent。同理用原始 ID:坐席是对客户端
        #    正在用的会话 ID 做接管;先换发会让接管被静默绕过。
        if hitl is not None and hitl.manual_mode.is_manual(req.session_id):
            return _reply_stream("🎧 当前会话已转由人工客服处理，请稍候…")

        # 3) 会话生命周期:确保 ID 可用;closed/未知(旧格式/伪造)→ 服务端换发翻篇
        active_id, rotated = ensure_active(get_db(), req.session_id, req.user_id)
        req.session_id = active_id   # 下游(锁/agent/存储/观测)全部用生效 ID
        get_db().touch_conversation(active_id)   # 标记最后活跃时间(工作台按此排序/显示)

        # 4) 规则快路径：高频简单意图秒回，跳过 Agent（省 LLM 成本）
        #    仍把这轮问答写进会话历史并落盘，保证刷新/切换后可回显（不因走快路径而丢失）。
        if settings.fast_path_enabled:
            fp = match_fast_path(req.message)
            if fp:
                agent = manager.get_or_create(req.session_id, req.user_id)
                with session_lock.guard(req.session_id) as got:
                    if not got:
                        # 抢不到锁=上一条还在处理:此时若照发快路径回复,该轮问答不会入历史
                        # (刷新/多端回显缺失),且与在途轮次并发改 raw_messages。故返回忙提示。
                        return _reply_stream("⏳ 您的上一条消息还在处理中，请稍候再发～",
                                             conversation=(active_id, rotated))
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
                return _reply_stream(fp["reply"], conversation=(active_id, rotated))

        # 5) 成本上限（防烧爆 API Key）
        if not cost_guard.allow():
            return _reply_stream("🛑 今日服务已达使用上限，请明天再来～")

        agent = manager.get_or_create(req.session_id, req.user_id)

        def event_stream():
            yield _sse_frame({"type": "conversation", "conversation_id": active_id,
                              "status": "rotated" if rotated else "active"})
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
                    hitl=hitl, confirm=req.confirm, hmdp_token=req.hmdp_token,
                    current_item_id=req.current_item_id,
                ):
                    yield _sse_frame(event)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/session/reset", dependencies=[Depends(admin_auth)])
    def reset(req: ResetRequest, request: Request):
        # 单一连续会话:重置=**清空同一条会话**(不关闭、不新建),继续用原 ID。
        req.user_id = _resolve_user(request, getattr(req, "user_id", "default"))
        # 只清本人自己的会话;拿不到规范会话则回落 open_or_reuse
        conv = get_db().get_conversation(req.session_id)
        sid = req.session_id if (conv and conv.get("user_id") == req.user_id) \
            else open_or_reuse(get_db(), req.user_id)["conversation_id"]
        manager.reset(sid)                      # 清空 agent 消息 + 热存储
        get_db().delete_session_snapshot(sid)   # 同清冷快照,避免历史回显残留旧消息
        return {"status": "reset", "conversation_id": sid}

    @app.post("/api/conversation/open")
    def conversation_open(req: OpenConversationRequest, request: Request):
        """服务端签发/复用会话:同用户已有 open 会话则复用(多端一致),否则新开。"""
        req.user_id = _resolve_user(request, req.user_id)
        return open_or_reuse(get_db(), req.user_id)

    @app.get("/api/conversations")
    def conversations_list(request: Request, user_id: str = "default", limit: int = 20):
        user_id = _resolve_user(request, user_id)
        return {"conversations": get_db().list_conversations(user_id, limit=limit)}

    @app.get("/api/session/{session_id}/history")
    def session_history(session_id: str, request: Request):
        """回显该会话已落盘的历史气泡(重启/刷新后聊天记录不再空白)。

        auth_enabled 时增归属校验:会话须存在且归属 token 用户,否则 403(旧格式 ID 也 403)。
        热存储(peek_messages)为空时(会话过期/清空)兜底读冷快照(S3),快照自身也带
        user_id 作二次归属防线。
        """
        uid = None
        if settings.auth_enabled:
            uid = _token_user(request)
            if uid is None:
                raise HTTPException(401, "未登录或登录已过期")
            conv = get_db().get_conversation(session_id)
            if conv is None or conv.get("user_id") != uid:
                raise HTTPException(403, "无权查看该会话")
        from app.api.history import reconstruct_bubbles
        messages = manager.peek_messages(session_id)
        if not messages and settings.session_snapshot_enabled:
            # 热存储已过期/清空 → 兜底冷快照(归属二次校验)
            try:
                snap = get_db().get_session_snapshot(session_id)
                if snap and (uid is None or snap.get("user_id") == uid):
                    messages = snap.get("messages") or []
            except Exception:
                messages = messages
        return {"session_id": session_id, "turns": reconstruct_bubbles(messages)}

    @app.post("/api/session/{session_id}/consolidate", dependencies=[Depends(admin_auth)])
    def consolidate(session_id: str, request: Request, user_id: str = "default"):
        """把本会话对话巩固进长期记忆(触发 Phase 5 策展),并回传当前长期记忆事实。

        巩固=把对话沉淀进长期记忆,是后台维护动作,**不结束会话**——用户可继续
        在同一会话聊天,记忆已沉淀且可重复巩固(幂等,策展去重)。结束会话(翻篇)
        由「重置对话」或空闲超时 reaper 负责,与巩固解耦。

        生产环境由空闲超时自动巩固(见 SessionManager.sweep/start_reaper);
        此端点是运维/演示用的手动触发,便于即时观察策展效果而不必等空闲 TTL。
        """
        user_id = _resolve_user(request, user_id)
        agent = manager.get_or_create(session_id, user_id)
        mm = getattr(agent, "memory_manager", None)
        if mm is None or not getattr(mm, "memory_enabled", False):
            return {"enabled": False, "count": 0, "facts": []}
        from app.observability.langfuse_bridge import background_trace
        # 与在途 /api/chat 用同一把会话锁互斥:先在锁内快照 raw_messages(防边写边读),
        # 再在锁外对不可变快照做慢巩固——不长期持锁阻塞用户(同 reaper 的"慢操作不持会话锁")。
        with session_lock.guard(session_id):
            msgs = list(getattr(agent, "raw_messages", []))
            summ = getattr(agent, "summary", None)
        # 手动巩固的 LLM 调用也归到命名 trace 下(与 reaper 自动巩固同名)
        with background_trace("consolidate_memory", session_id=session_id,
                              user_id=user_id,
                              input={"session_id": session_id, "trigger": "manual"}):
            mm.consolidate_to_long_term(msgs, summ)
        facts = [
            {"content": f.content, "category": f.category, "created_at": f.created_at}
            for f in mm.ltm.facts
        ]
        # 不再 close_conversation:巩固与结束会话解耦,巩固后会话继续。
        return {"enabled": True, "curation": settings.memory_curation_enabled,
                "count": len(facts), "facts": facts, "conversation_closed": False}

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
    def memory(request: Request, user_id: str = ""):
        """只读:从磁盘加载指定用户的长期记忆(反映真实存储,不触发巩固)。"""
        from app.agent.memory.long_term import LongTermMemory
        uid = _resolve_user(request, user_id or settings.memory_user_id)
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

    def _admin_load_messages(session_id: str) -> list:
        """坐席读会话消息:先热存储(peek),空则回退冷快照。与 /api/session/{id}/history 同源。"""
        messages = manager.peek_messages(session_id)
        if not messages and settings.session_snapshot_enabled:
            try:
                snap = get_db().get_session_snapshot(session_id)
                if snap:
                    messages = snap.get("messages") or []
            except Exception:  # noqa: BLE001
                pass
        return messages or []

    @app.get("/api/admin/conversations", dependencies=[Depends(admin_auth)])
    def admin_conversations(limit: int = 50):
        """坐席工作台:**每个客户只列一条**(其最活跃会话,与客户端登录复用的同一条),
        保证同一客户不出现多个窗口、且坐席看到的与客户端一致。"""
        out = []
        seen_users = set()
        # 拉足量后按用户去重;list 已按(open优先, 活跃时间倒序)排序,每个用户首次出现的即其规范会话
        for c in get_db().list_all_conversations(limit=500):
            uid = c.get("user_id")
            if uid in seen_users:
                continue
            seen_users.add(uid)   # 每个用户只取这一条(规范会话),后续同用户的碎片跳过
            sid = c["conversation_id"]
            msgs = _admin_load_messages(sid)   # 热存储空时回退快照,保证列表有预览
            user_msgs = [m for m in msgs if m.get("role") == "user"]
            if not user_msgs:
                continue   # 该客户规范会话尚无对话 → 暂不显示(有消息后自动出现)
            preview = user_msgs[-1]["content"]
            out.append({
                "conversation_id": sid,
                "user_id": c.get("user_id"),
                "status": c.get("status"),
                "created_at": c.get("created_at"),
                "last_active": c.get("updated_at") or c.get("created_at"),   # 最后活跃时间
                "manual": bool(hitl and hitl.manual_mode.is_manual(sid)),
                "preview": preview[:60],
                "turns": len(user_msgs),
            })
            if len(out) >= limit:
                break
        return {"conversations": out}

    @app.get("/api/admin/session/{session_id}/messages", dependencies=[Depends(admin_auth)])
    def admin_session_messages(session_id: str):
        """坐席读任意会话的气泡(管理网关已控权限,不做客户归属校验)。"""
        from app.api.history import reconstruct_bubbles
        return {"session_id": session_id,
                "turns": reconstruct_bubbles(_admin_load_messages(session_id))}

    @app.post("/api/admin/session/{session_id}/reply", dependencies=[Depends(admin_auth)])
    def admin_session_reply(session_id: str, req: AgentReplyRequest):
        """坐席以人工身份回复该会话:置人工模式 + 追加 assistant 气泡并落盘。"""
        text = (req.text or "").strip()
        if not text:
            raise HTTPException(422, "回复内容不能为空")
        conv = get_db().get_conversation(session_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")   # 不对未知会话静默建库,防误写
        uid = conv.get("user_id") or "default"
        agent = manager.get_or_create(session_id, uid)
        from app.api.history import reconstruct_bubbles
        with session_lock.guard(session_id) as got:
            if not got:
                raise HTTPException(409, "该会话正在处理中,请稍后再试")
            # 拿到锁后再转人工,避免抢锁失败(409)却已把会话切成人工态
            if hitl is not None and not hitl.manual_mode.is_manual(session_id):
                hitl.manual_mode.toggle(session_id)
            msgs = getattr(agent, "raw_messages", None)
            if isinstance(msgs, list):
                msgs.append({"role": "assistant", "content": json.dumps({
                    "intent": "human_agent", "confidence": 1.0, "reply": text,
                    "requires_human": False, "follow_up_question": None,
                }, ensure_ascii=False)})
                save = getattr(agent, "save", None)
                if callable(save):
                    try:
                        save()
                    except Exception:  # noqa: BLE001
                        pass
            bubbles = reconstruct_bubbles(getattr(agent, "raw_messages", []))
        get_db().touch_conversation(session_id)   # 人工回复也刷新活跃时间
        return {"status": "ok", "turns": bubbles}

    @app.get("/api/admin/skills", dependencies=[Depends(admin_auth)])
    def admin_skills():
        """自进化状态总览:现行技能 / 待审候选(含校验结论、风险档与放行策略) /
        各 skill 实战结局分布(含取样窗口披露) / 当前活跃灰度。

        candidates 只读 _candidates 目录,绝不在此处转正——转正只走
        app/scripts/promote_skill.py(校验+门禁+备份)。
        """
        from pathlib import Path as _Path

        from app.agent.skills.loader import SkillManager
        from app.agent.skills.risk import classify_risk, promotion_policy
        from app.scripts.promote_skill import CANDIDATES_DIR, DEFINITIONS_DIR, list_candidates

        live = SkillManager(skills_dir=settings.skills_dir, enabled=True).get_catalog()

        try:
            candidates = list_candidates(CANDIDATES_DIR, DEFINITIONS_DIR)
            for item in candidates:
                # 逐项容错:某一条候选的文件读不出/判不了风险,不该拖累整份列表——
                # 该候选仍要出现在响应里,只是 risk/policy 降级为 None。
                try:
                    content = _Path(item["path"]).read_text(encoding="utf-8")
                    item["risk"] = classify_risk(content, is_new_skill=not item["is_improvement"])
                    item["policy"] = promotion_policy(item["risk"])
                except Exception:  # noqa: BLE001
                    # None 在这里表示"判不了",不是"低危/可自动上线"——
                    # 下游必须把它当成需要人工复核处理,绝不能当作可自动转正。
                    item["risk"] = None
                    item["policy"] = None
        except Exception:  # noqa: BLE001 候选目录异常不该让总览 500
            candidates = []

        try:
            canaries = get_db().list_active_canaries()
        except Exception:  # noqa: BLE001
            canaries = []

        traces: dict[str, dict[str, int]] = {}
        try:
            for row in get_db().list_skill_traces(limit=_TRACE_WINDOW):
                bucket = traces.setdefault(row["skill_name"], {})
                outcome = row.get("outcome") or "unknown"
                bucket[outcome] = bucket.get(outcome, 0) + 1
        except Exception:  # noqa: BLE001
            traces = {}

        return {
            "live": live,
            "candidates": candidates,
            "traces": traces,
            "traces_window": {
                "limit": _TRACE_WINDOW,
                "note": "按最近轨迹计数的窗口值,非全时段统计;窗口为所有 skill 共用,高频 skill 可能挤占低频 skill 的样本",
            },
            "canaries": canaries,
        }

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
