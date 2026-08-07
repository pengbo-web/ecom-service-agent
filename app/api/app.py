"""FastAPI 应用工厂：SSE 流式对话 + 会话重置 + 静态前端 + 可观测性看板。"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.agent.tools.user_orders import STATUS_LABELS
from app.api.conversations import ensure_active, open_or_reuse
from app.api.schemas import (AgentReplyRequest, CartAddRequest, CartQuantityRequest, ChatRequest,
                              CreateOrderRequest, CreateUserRequest, LoginRequest,
                              OpenConversationRequest, ResetRequest, ReviewRequest,
                              SellerChatRequest, ShopProfileRequest, SkillDistillRequest)
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

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"
_DIST_DIR = _WEB_DIR / "dist"

# /api/admin/skills 的 traces 段查询窗口:按最近轨迹取样,全 skill 共用一个上限
# (高频 skill 可能挤占低频 skill 的样本)。响应里 traces_window.limit 直接引用
# 这个常量,保证"披露的数字"与"实际查询用的数字"不会走偏。
_TRACE_WINDOW = 500

# 上传技能包的体积上限(压缩包本身,解压后另有 bundle 模块的三重上限)
_MAX_UPLOAD_BYTES = 5_000_000
_UPLOAD_CHUNK_BYTES = 65536   # 分块读上传体的块大小(配合上限,避免整包先进内存)


def initial_order_status() -> str:
    """自助下单落库的初始状态,唯一由 `settings.unpaid_flow_enabled` 决定:
    开→`unpaid`(买家需再走一步 `pay_order` 才进入待发货);关→`pending`,
    与本特性上线前逐字节一致。模块级函数(而非 create_app 内的闭包)是为了
    在不起 FastAPI app 的情况下也能单独跑通开关测试。每次调用都现读
    `settings.unpaid_flow_enabled`,不在导入期把值固化下来。"""
    return "unpaid" if settings.unpaid_flow_enabled else "pending"


def _seller_factory(session_path: str, user_id: str | None = None):
    """卖家会话工厂:走 SellerOrchestrator(参谋/增长画像),而非买家的 MultiAgentOrchestrator。"""
    from app.multi_agent.orchestrator import SellerOrchestrator
    return SellerOrchestrator(session_path=session_path, user_id=user_id)


# 卖家会话与买家会话**完全隔离**:独立 SessionManager + 独立目录 + 独立锁字典。
# 否则店主与某个买家撞同一个 session_id 时,店主的话会落进买家会话里。
# 买家侧的 SessionManager 是 create_app() 内的局部变量(每次调用可注入/新建,
# 便于测试隔离);卖家侧不需要这种按次注入,故用模块级单例即可,
# 关键是 base_dir 与买家侧("app/sessions/api")永不相同。
seller_sessions = SessionManager(agent_factory=_seller_factory,
                                 base_dir="app/sessions/seller")


def _append_agent_reply(agent, text: str, intent: str = "human_agent",
                        strict_persist: bool = False) -> bool:
    """给某个已取到的会话 agent 追加一条结构化 assistant 回复并落盘。

    与 `POST /api/admin/session/{id}/reply` 共用的落地写法:reply 包一层与模型
    正常输出同构的 JSON 信封——`reconstruct_bubbles` 只认 assistant+JSON+`reply`
    字段,纯文本 assistant 消息会被当中间思考跳过,买家侧就看不到。调用方需
    已持有该会话的 session_lock 再调用本函数。返回是否真的追加成功
    (agent 没有 raw_messages 列表视为异常结构,返回 False,由调用方决定重试)。

    `strict_persist` 决定**落盘失败算不算失败**,两条路径的取舍刻意不同:

    - `False`(默认,坐席 `POST /api/admin/session/{id}/reply` 走这条):落盘
      失败静默吞掉,照旧返回 True。这在那条路径上是可接受的——响应体里回的是
      `reconstruct_bubbles(agent.raw_messages)`,坐席当场就能看见自己那条回复
      有没有进气泡流,而且他还坐在屏幕前,发现不对可以立刻重发。
    - `True`(触达投递走这条):落盘失败必须算失败,并且**把刚追加的那条消息
      从内存里撤回**。原因是触达没有人盯着:save() 失败时消息只活在内存里的
      agent 上,一旦会话被 idle reaper 淘汰或进程重启就彻底消失;而调用方
      (approve_draft)会据此把草稿标成 sent,`review_outreach_draft` 又只认
      还在 draft 的行 —— 于是这条消息既没送到、也永远重试不了,是静默的永久
      丢失。撤回内存里那条是为了让"失败"是干净的:调用方退回草稿后重试时,
      买家会话里不会残留一条半截的消息,重试也就不会变成发两遍。
    """
    msgs = getattr(agent, "raw_messages", None)
    if not isinstance(msgs, list):
        return False
    entry = {"role": "assistant", "content": json.dumps({
        "intent": intent, "confidence": 1.0, "reply": text,
        "requires_human": False, "follow_up_question": None,
    }, ensure_ascii=False)}
    msgs.append(entry)
    save = getattr(agent, "save", None)
    if not callable(save):
        # 严格模式下"根本没有落盘能力"与"落盘失败"是同一件事:这条消息不会被
        # 持久化,不能报成功。宽松模式保持既有行为(直接 True)。
        if strict_persist:
            if msgs and msgs[-1] is entry:
                msgs.pop()
            logger.error("触达投递:会话对象没有可调用的 save(),无法保证持久化,已撤回追加")
            return False
        return True
    try:
        save()
    except Exception:  # noqa: BLE001
        if not strict_persist:
            pass           # 与既有 /reply 端点行为一致:落盘失败静默,
                           # 内存里的消息已追加,不因落盘异常打断响应
        else:
            if msgs and msgs[-1] is entry:
                msgs.pop()
            logger.exception("触达投递落盘失败,已撤回内存中的追加(本次投递按失败处理)")
            return False
    return True


def _sse_frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _skill_canary_block(skill_name: str) -> str | None:
    """该 skill 正在灰度中 → 返回给操作者看的中文拒绝理由;不在灰度 → None。

    为什么两个写候选的端点都必须先问这一句:灰度期 `loader._canary_dir` 是**每次
    load_skill 都从磁盘重读候选目录**的,所以覆盖 `_candidates/<name>/` 等于把
    未经审核的内容**立刻**推给正在被分流的真实顾客会话(默认 50%)。响应里那句
    「风险=high,需人工」是在那份文本已经上线之后才写出来的,拦不住任何东西。
    铁律是"转正是唯一的上线路径",上传/蒸馏都不能从侧面绕开它。

    DB 读不出来时按**有灰度**处理(fail-closed):判不了就别写。
    """
    try:
        canary = get_db().get_active_canary(skill_name)
    except Exception:  # noqa: BLE001 判不了就拒写,不能默认放行
        return (f"无法确认技能「{skill_name}」当前是否正在灰度(数据库读取失败),"
                "出于安全考虑已拒绝写入候选目录。请稍后重试或联系管理员。")
    if not canary:
        return None
    return (f"技能「{skill_name}」正在灰度中(分流 {canary.get('percent')}%,候选正文"
            "正在为一部分真实顾客会话服务)。此时覆盖候选目录会让未经审核的内容"
            "**立刻**进入线上对话,故已拒绝写入。请先结束这轮灰度再上传:"
            "`python -m app.scripts.skill_watchdog --check` 收口,或 "
            f"`python -m app.scripts.promote_skill {skill_name} --rollback` 回滚。")


def _process_skill_upload(raw: bytes, filename: str) -> dict:
    """上传技能包的**阻塞段**:解压 → 整树校验 → 判档 → 换目录。由线程池调用。

    单独拆出来的原因见 admin_upload_skill 的 docstring(事件循环不能被这段占住)。
    返回值就是端点的响应体。
    """
    import shutil
    import tempfile

    from app.agent.skills.bundle import BundleError, extract_skill_bundle
    from app.agent.skills.risk import promotion_policy
    from app.agent.skills.tree_text import classify_tree_risk, validate_skill_tree
    from app.scripts import promote_skill as ps

    def _reject(errors: list[str], unknown: list[str] | None = None) -> dict:
        return {"accepted": False, "name": "", "replaced": False, "risk": None,
                "policy": None, "files": [], "errors": errors,
                "unknown_tools": unknown or []}

    lower = (filename or "").lower()
    tmp_root = Path(tempfile.mkdtemp(prefix="skill_upload_"))
    try:
        if lower.endswith(".zip"):
            try:
                info = extract_skill_bundle(raw, str(tmp_root))
            except BundleError as exc:
                return _reject([str(exc)])
            files = info["files"]
        else:
            # 单个 SKILL.md:视作只含一份说明的技能包
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return _reject(["文件不是 UTF-8 文本(技能包请打成 .zip)"])
            (tmp_root / "SKILL.md").write_text(text, encoding="utf-8")
            files = []

        # 校验**整棵技能树**:附带资料会随转正一起进正式目录,并由 read_skill_file
        # 整段灌进模型上下文 —— 只审根 SKILL.md 等于给"正文人畜无害、附件里写着
        # 直接全额退款"留一条没人看的暗道。
        report = validate_skill_tree(tmp_root)
        tree = report["tree"]
        if "SKILL.md" in tree["unreadable"]:
            # 中文操作者在 Windows 上把 SKILL.md 存成 GBK 是最常见的坏上传,
            # 它属于"校验不通过",按 docstring 走 200 + accepted=false,不是 500
            return _reject(["SKILL.md 不是 UTF-8 文本或无法读取(Windows 上请另存为 "
                            "UTF-8,不要用 GBK/ANSI 编码),已拒收。"])
        if not report["valid"]:
            return _reject(report["errors"], report["unknown_tools"])

        name = report["name"]
        # 残留竞态(已知、按要求不加锁,记在这里而不是装作不存在):`_skill_canary_block`
        # 在这一行查过"没有活跃灰度",`ps._replace_tree` 要再等几行(整树 copytree
        # 之后)才真正写盘。如果 `skill_watchdog --start` 恰好在这几行中间对同一个
        # 技能开了灰度,这次上传仍会覆盖候选目录、灰度看到的会是被换掉的内容。
        # 窗口只有一次内存校验 + 一次同卷 copytree 的时长,比 C1 关掉的那个
        # "整段请求处理期间"窗口小两三个数量级,而且触发条件极窄(必须与
        # --start 命中同一个技能名的同一瞬间);与 skill_watchdog.py 里
        # classify→db.start_canary 那处同级残留窗口一样,接受它而不是加锁。
        blocked = _skill_canary_block(name)
        if blocked:
            return _reject([blocked])

        dest = Path(ps.CANDIDATES_DIR) / name
        replaced = (dest / "SKILL.md").exists()

        is_new = not (Path(ps.DEFINITIONS_DIR) / name / "SKILL.md").exists()
        risk = classify_tree_risk(tmp_root, is_new_skill=is_new, tree=tree)

        # 复用转正链路那套"同卷 _swap + 两次 rename"的换目录:临时目录在系统盘,
        # 直接 shutil.move 会退化成跨卷复制,期间 _candidates/<name>/ 可能是空的或
        # 只写了一半 —— 而 admin_skills 与 promote_skill(--list/promote)正并发读它。
        ps._replace_tree(tmp_root, dest)

        return {"accepted": True, "name": name, "replaced": replaced,
                "risk": risk, "policy": promotion_policy(risk),
                "files": files, "errors": [], "unknown_tools": []}
    finally:
        if tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)


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
    # 挂到 app.state 供测试/运维按需内省(如校验卖家会话与买家会话确系两套
    # 独立 SessionManager);不改变 manager 本身的构造与行为。
    app.state.session_manager = manager

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
        """前端启动读取:demo_mode 开时前端自动登录 demo_user_id、跳过登录卡片。

        unpaid_flow_enabled 供前端决定购物车 Tab / 支付按钮要不要出现——开关
        关闭时前端必须退回改造前的样子(不出现购物车入口/待支付/去支付),
        而不是仅靠"永远不会有 unpaid 订单"这个后端事实来隐式兜底。"""
        return {"demo_mode": settings.demo_mode,
                "demo_user_id": settings.demo_hmdp_user_id if settings.demo_mode else "",
                "unpaid_flow_enabled": settings.unpaid_flow_enabled}

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
            "status_label": STATUS_LABELS.get(o.get("status"), o.get("status_text") or o.get("status")),
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
        """自助下单:用户在商城/商品卡点『立即购买』(或购物车「去下单」)→ 按登录身份建单。
        demo 用户 → 建到 hmdp(hmdp 自身已有真实支付流程,不受本开关影响,状态
        恒为待支付);其它 → agent 订单库,初始状态由 initial_order_status() 按
        settings.unpaid_flow_enabled 决定(开=unpaid 需再付款,关=pending,与
        改造前逐字节一致)。下单成功后把该商品在本地购物车里的 active 行标记为
        converted——不论这次下单是从购物车「去下单」发起,还是商品卡「立即购买」
        绕开购物车直接下的单,买家事实上都已经为这件商品完成了下单。"""
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
            try:
                get_db().mark_cart_converted(user, [req.item_id])
            except Exception:  # noqa: BLE001 购物车状态清理失败不影响已下单结果
                pass
            return {"success": True, "order_id": d.get("data"), "status_label": "待支付", "total": total}
        order = get_db().create_order(
            user=user,
            items=[{"name": p["title"], "sku": f"HMDP-{req.item_id}", "quantity": qty, "price": p["price"]}],
            total=total, status=initial_order_status(), shipping_address=req.shipping_address,
        )
        try:
            get_db().mark_cart_converted(user, [req.item_id])
        except Exception:  # noqa: BLE001 购物车状态清理失败不影响已下单结果
            pass
        return {"success": True, "order_id": order["order_id"],
                "status_label": STATUS_LABELS.get(order["status"], order["status"]),
                "total": order["total"]}

    @app.post("/api/order/{order_id}/pay")
    def pay_order_endpoint(order_id: str, request: Request):
        """买家为自己的未支付订单完成支付(unpaid → pending)。这是**买家自己**
        的动作,不是 Agent 工具——与"不代客下单"同一条底线,故只做端点。归属
        与幂等都下沉到 Database.pay_order 的条件更新里:付别人的单、或对已经
        支付过的订单重复调用,都返回 False,不改变订单状态。"""
        user = _resolve_user(request, None)
        ok = get_db().pay_order(order_id, user)
        if not ok:
            raise HTTPException(400, "支付失败:订单不存在、不属于当前用户,或已完成支付")
        order = get_db().get_order(order_id)
        return {"success": True, "order_id": order_id, "status": order["status"],
                "status_label": STATUS_LABELS.get(order["status"], order["status"])}

    @app.post("/api/cart")
    def add_to_cart_endpoint(req: CartAddRequest, request: Request):
        """加入购物车。**不做结算**——下单仍走 POST /api/order 既有自助下单路径。"""
        user = _resolve_user(request, None)
        sku = (req.item_id or "").strip()
        if not sku:
            raise HTTPException(422, "商品ID不能为空")
        qty = max(1, min(int(req.quantity or 1), 99))
        get_db().add_to_cart(user, sku, qty)
        return {"success": True}

    @app.get("/api/cart")
    def get_cart_endpoint(request: Request):
        """当前用户的购物车(供「购物车」页)。"""
        user = _resolve_user(request, None)
        return {"success": True, "items": get_db().list_cart(user)}

    @app.put("/api/cart/{sku}")
    def set_cart_quantity_endpoint(sku: str, req: CartQuantityRequest, request: Request):
        """把购物车里某个 sku 的数量设置为一个具体值(而不是累加)。前端的
        「改数量」+/- 控件统一走这一个端点(而不是加/减各挂一条不同路径)——
        减少数量如果拆成"先删再按新数量加回",第二次调用失败就会把买家的
        购物车项凭空丢掉,两次网络往返也没有原子性。数量必须是正整数,
        非正数明确拒绝而不是静默删除;「设为 0」与「移除」是两件事,移除
        请显式调用 DELETE。sku 不在购物车里同样不是"设置成功"的一种,
        返回 404 明确告知,而不是隐式创建一行(新增走 POST /api/cart)。"""
        user = _resolve_user(request, None)
        qty = int(req.quantity)
        if qty <= 0:
            raise HTTPException(422, "数量必须是正整数;如需移除该商品请使用移除功能")
        ok = get_db().set_cart_quantity(user, sku, qty)
        if not ok:
            raise HTTPException(404, f"购物车里没有商品 {sku},无法设置数量")
        return {"success": True}

    @app.delete("/api/cart/{sku}")
    def remove_cart_item_endpoint(sku: str, request: Request):
        user = _resolve_user(request, None)
        ok = get_db().remove_from_cart(user, sku)
        return {"success": ok}

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

    @app.get("/api/reviewable")
    def reviewable(request: Request):
        """当前买家可评价的已签收订单项。"""
        uid = _resolve_user(request, None)
        return {"success": True, "items": get_db().reviewable_items(uid)}

    @app.post("/api/review")
    def submit_review(req: ReviewRequest, request: Request):
        """买家提交评价。一单一 sku 一次;重复给明确中文提示而不是 500。"""
        uid = _resolve_user(request, None)
        if not (1 <= int(req.rating) <= 5):
            raise HTTPException(status_code=400, detail="评分需在 1-5 之间")
        rid = get_db().create_review(req.order_id, uid, req.sku,
                                     int(req.rating), (req.content or "").strip())
        if rid is None:
            return {"success": False, "reason": "这笔订单的该商品已经评价过了,不能重复评价。"}
        return {"success": True, "review_id": rid}

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
        # 重置会清空这段对话:先归档,否则这段内容就永久丢失、学不到任何东西
        try:
            agent = manager.get_or_create(sid, req.user_id)
            get_db().archive_session_if_changed(
                session_id=sid, user_id=req.user_id,
                messages=list(getattr(agent, "raw_messages", []) or []),
                summary=getattr(agent, "summary", None))
        except Exception:  # noqa: BLE001 归档 best-effort,失败不阻断重置
            pass
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
            _append_agent_reply(agent, text)
            bubbles = reconstruct_bubbles(getattr(agent, "raw_messages", []))
        get_db().touch_conversation(session_id)   # 人工回复也刷新活跃时间
        return {"status": "ok", "turns": bubbles}

    @app.post("/api/admin/sessions/archive", dependencies=[Depends(admin_auth)])
    def admin_archive_sessions():
        """把当前内存里的活跃会话立刻冷归档(供离线自进化闭环使用)。

        为什么需要:归档原本只发生在会话被 TTL 淘汰时(默认 30 天),而失败驱动的
        skill 改进需要 traces 与 archives 相交 —— 等淘汰意味着最快 30 天后才学得到。
        离线跑闭环前先打这个接口,即可把刚发生的真实对话纳入学习范围。
        幂等:内容没增长的会话会被跳过(archive_session_if_changed)。
        """
        archived, skipped = [], []
        for sid, agent in manager.snapshot_agents():
            try:
                ok = get_db().archive_session_if_changed(
                    session_id=sid,
                    user_id=getattr(agent, "user_id", "default"),
                    messages=list(getattr(agent, "raw_messages", []) or []),
                    summary=getattr(agent, "summary", None))
                (archived if ok else skipped).append(sid)
            except Exception:  # noqa: BLE001 单个会话失败不影响其余
                skipped.append(sid)
        return {"archived": len(archived), "skipped": len(skipped),
                "archived_sessions": archived}

    @app.get("/api/admin/skills", dependencies=[Depends(admin_auth)])
    def admin_skills():
        """自进化状态总览:现行技能 / 待审候选(含校验结论、风险档与放行策略) /
        各 skill 实战结局分布(含取样窗口披露) / 当前活跃灰度。

        candidates 只读 _candidates 目录,绝不在此处转正——转正只走
        app/scripts/promote_skill.py(校验+门禁+备份)。
        """
        from pathlib import Path as _Path

        from app.agent.skills.loader import SkillManager
        from app.agent.skills.risk import promotion_policy
        from app.agent.skills.tree_text import classify_tree_risk, read_skill_tree
        from app.scripts.promote_skill import CANDIDATES_DIR, DEFINITIONS_DIR, list_candidates

        live = SkillManager(skills_dir=settings.skills_dir, enabled=True).get_catalog()

        try:
            candidates = list_candidates(CANDIDATES_DIR, DEFINITIONS_DIR)
            for item in candidates:
                # 逐项容错:某一条候选的文件读不出/判不了风险,不该拖累整份列表——
                # 该候选仍要出现在响应里,只是 risk/policy 降级为 None。
                try:
                    # 判档看**整棵候选目录**(含附带资料):面板上那枚风险徽标是操作者
                    # 决定"要不要人工介入"的依据,它必须覆盖会真正上线的全部文本。
                    tree = read_skill_tree(_Path(item["path"]).parent)
                    if tree["unreadable"] or tree["over_cap"]:
                        # 这里是只读面板,故不像 promote/watchdog 那样把"审不了"折成
                        # high(那会谎报"检出了动钱内容"),而是如实降级为 None ——
                        # 前端把 None 渲染成「未判定 · 需人工复核」,同样不会被自动放行。
                        raise ValueError("候选目录存在无法审核的文件")
                    item["risk"] = classify_tree_risk(
                        _Path(item["path"]).parent,
                        is_new_skill=not item["is_improvement"], tree=tree)
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

    @app.post("/api/admin/skills/upload", dependencies=[Depends(admin_auth)])
    async def admin_upload_skill(file: UploadFile = File(...)):
        """上传技能包(.zip)或单个 SKILL.md,作为**候选**(绝不直写正式目录)。

        上传内容与 LLM 生成的候选同级不可信,故走同一套关卡:安全解压(防穿越/
        炸弹/符链)+ validate_candidate(frontmatter 完整 + 工具名真实 + 名字是
        安全路径段)。技能名只取**校验后 frontmatter 里的 name**,不取包内目录名、
        不取上传文件名——多一个可控的路径来源就是多一个信任面。

        流程:先解压到临时目录并校验,全部通过才整目录移进 _candidates/<name>/。
        校验不通过返回 200 + accepted=false + 错误列表(前端统一渲染);
        4xx 只留给鉴权与体积超限。

        该 skill **正在灰度中就一律拒收**:灰度是每次 load_skill 从磁盘重读候选
        目录的,覆盖它等于把未审内容直接推给线上会话(详见 _skill_canary_block)。

        并发:本端点只有"读 multipart"必须 await,解压(最多 10 MB zlib 解压)、
        逐文件写盘、跨卷 copytree 全是同步阻塞活。本服务的主业是 SSE 流式客服
        对话,把这些放在事件循环上跑,一次 5 MB 上传就会冻住所有在途的流,
        所以阻塞段整段丢进线程池(run_in_threadpool),端点本身仍是 async。
        """
        # 分块读并随读随判:一次性 file.read() 会先把整个请求体读进内存,
        # 那样体积上限根本约束不到内存占用(本栈其它地方也没有请求体大小限制)。
        buf = bytearray()
        while True:
            chunk = await file.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > _MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"上传过大(上限 {_MAX_UPLOAD_BYTES} 字节)")

        return await run_in_threadpool(_process_skill_upload, bytes(buf), file.filename or "")

    @app.post("/api/admin/skills/distill", dependencies=[Depends(admin_auth)])
    def admin_distill_skill(req: SkillDistillRequest):
        """上传客服 SOP / 产品资料,让 LLM 提炼成**候选**技能(不直接上线)。

        会真调一次 LLM(花钱),前端须二次确认。资料是不可信外部输入,故:
        prompt 给正文加围栏 + 产物过 validate_candidate + 只落 _candidates/ 且带
        风险档 —— 即便资料里藏了注入,产出也进不了正式目录。

        写盘分两步:先蒸馏到**临时暂存目录**,确认该技能名没有活跃灰度后,才整目录
        换进 `_candidates/<name>/`。技能名要等 LLM 产出并过校验才知道,所以灰度
        检查只能排在调用之后、写入之前 —— 但"写入候选目录"这一步一定在检查之后
        (灰度期覆盖候选 = 未审内容直接上线,见 _skill_canary_block)。
        """
        import shutil
        import tempfile

        from openai import OpenAI

        from app.agent.skills import doc_distill as dd
        from app.agent.skills.risk import promotion_policy
        from app.agent.skills.tree_text import classify_tree_risk, validate_skill_tree
        from app.scripts import promote_skill as ps

        doc = (req.doc_text or "").strip()
        if not doc:
            return {"created": False, "name": None, "risk": None, "policy": None,
                    "errors": ["资料正文为空"], "truncated": False}
        if len(doc) > dd.MAX_DOC_CHARS * 4:
            raise HTTPException(413, f"资料过大(建议先精简到 {dd.MAX_DOC_CHARS} 字符以内)")

        # 只截断参与蒸馏的正文,不改变上面 4 倍上限的拒绝口径;操作者必须被如实告知
        # "贴的内容有一截没真正喂给模型",否则一份 30000 字的 SOP 悄悄丢了尾部
        truncated = dd.is_doc_truncated(doc)

        staging = Path(tempfile.mkdtemp(prefix="skill_distill_"))
        try:
            try:
                client = OpenAI(api_key=settings.openai_api_key,
                                base_url=settings.openai_base_url)
                out = dd.distill_from_doc(client, settings.model_name, doc, str(staging))
            except Exception as exc:  # noqa: BLE001 LLM/网络失败如实回传,不 500
                # 完整异常(可能带 base_url/代理等细节)只落服务端日志,回给客户端的只有类型名
                logger.exception("skills/distill 调用 LLM 失败")
                return {"created": False, "name": None, "risk": None, "policy": None,
                        "errors": [f"蒸馏失败，请稍后重试或联系管理员（{type(exc).__name__}）"],
                        "truncated": truncated}

            if out is None:
                return {"created": False, "name": None, "risk": None, "policy": None,
                        "errors": ["LLM 产物未通过校验(frontmatter 不全 / 工具名不实 / 名字非法)"],
                        "truncated": truncated}

            name = out["name"]
            blocked = _skill_canary_block(name)
            if blocked:
                return {"created": False, "name": name, "risk": None, "policy": None,
                        "errors": [blocked], "truncated": truncated}

            # 校验 + 判档都看整棵树(与上传端点同口径)。蒸馏产物眼下只有一份
            # SKILL.md、`distill_from_doc` 内部也已经用 `validate_candidate` 校验
            # 过同一份正文,这里再校验一遍在今天看来是重复的——但上传端点两者都跑,
            # 蒸馏端点只跑判档不跑校验,正是终审点名的"同一件事两套标准";不校验
            # 一次就不该只因为"产物目前恰好没有附件"而心存侥幸。
            tree_report = validate_skill_tree(staging / name)
            if not tree_report["valid"]:
                return {"created": False, "name": name, "risk": None, "policy": None,
                        "errors": tree_report["errors"], "truncated": truncated}

            is_new = not (Path(ps.DEFINITIONS_DIR) / name / "SKILL.md").exists()
            risk = classify_tree_risk(staging / name, is_new_skill=is_new,
                                      tree=tree_report["tree"])

            ps._replace_tree(staging / name, Path(ps.CANDIDATES_DIR) / name)
            return {"created": True, "name": name, "risk": risk,
                    "policy": promotion_policy(risk), "errors": [], "truncated": truncated}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _require_seller_console() -> None:
        """控制台关掉时给 404 而不是 500——功能不存在是 404 的语义,不是服务器出错。"""
        if not getattr(settings, "seller_console_enabled", True):
            raise HTTPException(status_code=404, detail="卖家控制台未启用")

    @app.post("/api/seller/chat", dependencies=[Depends(admin_auth)])
    def seller_chat(req: SellerChatRequest):
        """店主与卖家侧 Agent 对话(参谋/增长由 SellerRouter 内部按话题决定)。

        鉴权用 admin_auth:经营数据只对店铺管理者开放,买家 token 拿不到。
        会话落进 seller_sessions(独立 SessionManager+独立目录),与买家会话
        的 session_id 命名空间互不相通,同名 session_id 不会串话。
        """
        _require_seller_console()
        sid = (req.session_id or "").strip() or "seller-default"
        orch = seller_sessions.get_or_create(sid, user_id="seller")
        lock = seller_sessions.get_lock(sid)
        with lock:
            result = orch.chat(req.message or "")
            try:
                orch.save()
            except Exception:      # noqa: BLE001 落盘失败不吞掉已生成的回复
                logger.exception("卖家会话落盘失败 sid=%s", sid)
        # SellerOrchestrator.chat() 委托给 EcomAgent.chat(),后者恒返回
        # CustomerServiceResponse **pydantic 对象**(与买家侧 streaming.py
        # 的 result.reply 同一读法),从不是 dict——这里要用属性访问,
        # 不能当 dict 用 .get(),否则永远走不到分支,只会把整个对象的 repr
        # 字符串化后当成回复发给店主。
        reply = result.reply
        key = getattr(orch, "last_agent_key", "analyst")
        from app.multi_agent.agents import SELLER_AGENT_CONFIGS
        return {"success": True, "reply": reply, "agent_key": key,
                "agent": SELLER_AGENT_CONFIGS.get(key, {}).get("name", key),
                "session_id": sid}

    @app.get("/api/seller/overview", dependencies=[Depends(admin_auth)])
    def seller_overview(window_days: int = 7):
        """控制台首屏:经营总览 + 商品诊断 + 当前异常 + 服务质量(含情绪分布)。

        全只读,不调用 LLM,可高频轮询刷新。`quality` 是 N2 新增的附加键
        (不动既有 overview/products/anomalies),前端「经营诊断」的情绪分布卡
        靠它拿到 service_quality() 里的 emotion 段。
        """
        _require_seller_console()
        from app.agent.tools.anomaly import anomaly_scan
        from app.agent.tools.reviews import review_insights
        from app.agent.tools.shop_analytics import (
            product_diagnostics, service_quality, shop_overview)
        return {
            "overview": shop_overview(window_days=window_days),
            "products": product_diagnostics(window_days=window_days, top_n=5),
            "anomalies": anomaly_scan(window_days=window_days)["anomalies"],
            "quality": service_quality(window_days=window_days),
            "reviews": review_insights(window_days=window_days),
        }

    @app.get("/api/admin/shop/profile", dependencies=[Depends(admin_auth)])
    def get_shop_profile_api():
        """店主读取当前店铺人格(语气/称呼/禁语)。fail-soft:读不到返回默认。"""
        _require_seller_console()
        from app.config.shop_profile import DEFAULT_TONE, MAX_TONE_CHARS, load_profile
        p = load_profile()
        return {"success": True, "profile": p,
                "default_tone": DEFAULT_TONE, "max_tone_chars": MAX_TONE_CHARS}

    @app.put("/api/admin/shop/profile", dependencies=[Depends(admin_auth)])
    def put_shop_profile_api(req: ShopProfileRequest):
        """店主保存店铺人格。留空 tone = 恢复默认(不是错误);下一轮对话起生效。"""
        _require_seller_console()
        from app.config.shop_profile import validate_tone
        ok, why = validate_tone(req.tone or "")
        if not ok:
            raise HTTPException(status_code=400, detail=why)
        get_db().set_shop_profile(
            {"shop_name": (req.shop_name or "").strip(),
             "tone": (req.tone or "").strip(),
             "banned_words": (req.banned_words or "").strip()},
            updated_by="admin")
        return {"success": True}

    def _deliver_outreach(draft: dict) -> dict:
        """把一条已批准的触达草稿投递给买家。**绝不抛**。

        返回 `{"delivered": bool, "warning": str}`:
        - `delivered=False`:消息**没有**进买家的会话,调用方可以放心退回草稿重试;
        - `delivered=True, warning=""`:干净成功;
        - `delivered=True, warning=非空`:消息**已经**进了买家的会话(不可撤销),
          但之后某个记账动作失败了,操作者需要知道,但**绝不能据此重试**。

        为什么必须区分后两者:`_append_agent_reply` 一旦返回 True,这条消息就已经
        在买家的聊天记录里了。此后再发生的任何异常(例如 `touch_conversation` 撞上
        SQLite 的 5 秒 busy timeout —— 协作 worker 正在写同一个库文件时完全可能)
        如果被折算成"投递失败",approve_draft 就会把草稿退回 draft"以便重试",
        而操作者的重试会给同一个买家**再追加一条一模一样的消息**。分支承诺的是
        "没有人工不发,发也绝不发两遍",所以这里的纪律是:**追加成功之后的每一步
        都不得改变 delivered 的取值**,只能往 warning 里记。这与 approve_draft 对
        `mark_outreach_sent` 的处理(已投递 + 账本没对上 = success:false / sent:true)
        是同一套口径,只是那一层在下游,这一层不能把信息提前塌掉。

        复用 `POST /api/admin/session/{id}/reply` 的落地路径:同一把
        session_lock、同一个（本 app 实例的）session_manager 里取 agent、用
        `_append_agent_reply` 追加同构的 assistant 回复并落盘——买家在自己的
        聊天里看到这条消息,与坐席人工回复走同一条通道,不新造一套没人审的
        消息出口。这里传 `strict_persist=True`:触达没人盯着,落盘失败必须算失败
        (理由见 `_append_agent_reply` 的 docstring)。用 latest_conversation 找该
        买家的规范会话;找不到会话或抢不到锁都视为失败,由 approve 端点退回 draft
        状态,可重试。
        """
        # 这个标志就是上面那条纪律的代码形态:一旦置 True,本函数**任何**出口都
        # 必须报 delivered=True。不靠"把危险的语句摆在 try 之外"来保证——那种写法
        # 只对当时想到的那一句有效,而 `with session_lock.guard(...)` 的退出
        # (Redis 后端释放锁)同样可能抛,它就在 try 内、且在追加**之后**。
        appended = False
        sid = ""
        db = None
        try:
            db = get_db()
            user_id = draft.get("user_id", "")
            conv = db.latest_conversation(user_id)
            if not conv:
                return {"delivered": False, "warning": ""}
            sid = conv["conversation_id"]
            with session_lock.guard(sid) as got:
                if not got:
                    # 抢不到锁:上层退回待审,可重试
                    return {"delivered": False, "warning": ""}
                agent = manager.get_or_create(sid, user_id)
                if not _append_agent_reply(agent, draft.get("content", ""),
                                            intent="growth_outreach",
                                            strict_persist=True):
                    return {"delivered": False, "warning": ""}
                appended = True
        except Exception:  # noqa: BLE001 投递失败要能被上层退回重试,不能炸成 500
            logger.exception("触达投递失败 draft=%s(已追加=%s)",
                             draft.get("id"), appended)
            if not appended:
                return {"delivered": False, "warning": ""}
            return {"delivered": True,
                    "warning": (f"消息已投递给买家(会话 {sid}),但投递收尾时发生异常;"
                                "买家已收到的消息不受影响,**请勿重试**,请查日志核实")}

        # ↓↓↓ 这条线以下,消息已经在买家会话里了。下面每一步都必须是**非致命**的:
        # 只能把问题写进 warning,不能让 delivered 变回 False。
        warning = ""
        try:
            db.touch_conversation(sid)
        except Exception:  # noqa: BLE001 记账失败不得反转"已投递"这个事实
            logger.exception(
                "触达消息已投递给买家,但刷新会话活跃时间失败 draft=%s sid=%s"
                "(不影响投递结果,不得据此重试)", draft.get("id"), sid)
            warning = (f"消息已投递给买家,但刷新会话活跃时间(conversation {sid})失败;"
                       "这只影响会话列表的排序,不影响买家已收到的消息,**请勿重试**")
        return {"delivered": True, "warning": warning}

    def _delivery_result(raw) -> tuple[bool, str]:
        """把投递函数的返回值归一成 (delivered, warning)。

        `app.state.deliver_outreach` 是一个可替换的注入点(测试打桩、未来别的
        投递通道),历史签名返回裸 bool。裸 bool 只表达"送没送到",没有"送到了但
        记账有问题"这一档,按 warning 为空处理即可,语义无损且不会误判成失败。
        """
        if isinstance(raw, dict):
            return bool(raw.get("delivered")), str(raw.get("warning") or "")
        return bool(raw), ""

    # 挂到 app.state,而不是靠 `global` 改写模块级名字:这里闭包住的是**这个**
    # app 实例的 manager/session_lock,与买家侧 session_manager 同样挂
    # app.state 是同一个理由(见上面 app.state.session_manager 处的注释)——
    # 同进程里先后建出的多个 app 实例,各自的 app.state.deliver_outreach 互不
    # 覆盖;approve_draft 通过闭包住的 `app` 变量按实例取用,测试改为
    # monkeypatch.setattr(client.app.state, "deliver_outreach", ...) 打桩。
    app.state.deliver_outreach = _deliver_outreach

    @app.get("/api/admin/growth/drafts", dependencies=[Depends(admin_auth)])
    def growth_drafts(status: str = "draft", limit: int = 50):
        """列触达草稿(默认只看待审的)。供人工审批控制台使用。

        每条附上 opportunity_label:商机类型的中文名由**后端唯一持有**
        (app/agent/tools/growth.py 的 OPPORTUNITY_KINDS),前端只负责显示。
        否则前端得自己抄一份映射表——本特性里 kind 已经改名过一次
        (unpaid_order → stale_pending_order),抄的那份不同步就会静默退回
        给店主显示英文标识符。未知类型回落原始 kind,不留空白。

        草稿带 offer.coupon_code 时同样附上 coupon_discount(N6):文案(如
        「满300减30」)唯一来自 app.agent.coupons.grants.COUPON_BY_CODE
        (再往上追溯就是 order_ops._COUPONS)——把"发出去要真的发一张券"这件事
        在人工点批准**之前**摆到界面上,而不是让店主事后才知道。券码不在
        COUPON_BY_CODE 里(模型编的)时留空,前端据此提示这不是一张真实的券。
        """
        _require_seller_console()
        from app.agent.tools.growth import OPPORTUNITY_KINDS
        from app.agent.coupons.grants import COUPON_BY_CODE

        rows = get_db().list_outreach_drafts(status=status or None, limit=limit)
        for r in rows:
            kind = r.get("opportunity_type") or ""
            r["opportunity_label"] = OPPORTUNITY_KINDS.get(kind, kind)
            code = (r.get("offer") or {}).get("coupon_code") or ""
            if code:
                info = COUPON_BY_CODE.get(code)
                r["coupon_discount"] = info["discount"] if info else ""
        return {"success": True, "drafts": rows}

    @app.get("/api/admin/growth/opportunity-kinds", dependencies=[Depends(admin_auth)])
    def growth_opportunity_kinds():
        """商机类型全集及其中文标签,供「商机概览」卡片渲染。

        唯一口径同样是 app/agent/tools/growth.py 的 OPPORTUNITY_KINDS——前端
        不再另抄一份 kind→label 的表:那份手抄表已经在这个代码库里因为漏同步
        坑过三次(草稿卡标签、校验器工具清单、以及这张商机概览卡本身),这个
        端点就是让"新增一个 kind"只需要改这一处,前端零改动就能显示出来。
        """
        _require_seller_console()
        from app.agent.tools.growth import OPPORTUNITY_KINDS
        kinds = [{"kind": k, "label": v} for k, v in OPPORTUNITY_KINDS.items()]
        return {"success": True, "kinds": kinds}

    @app.get("/api/admin/growth/opportunities", dependencies=[Depends(admin_auth)])
    def growth_opportunities(kind: str = "stale_pending_order", window_days: int = 14):
        """只读地找一批增长商机(不落草稿),供人工/营销 Agent 参考。"""
        _require_seller_console()
        from app.agent.tools.growth import find_opportunities
        out = find_opportunities(kind=kind, window_days=window_days)
        if not out.get("success"):
            raise HTTPException(status_code=400, detail=out.get("error", "参数错误"))
        return out

    @app.get("/api/admin/growth/outreach-stats", dependencies=[Depends(admin_auth)])
    def growth_outreach_stats(window_days: int = 30):
        """触达转化归因统计(N3):已发送 / 已转化 / 转化率,供工作台「触达效果」卡。

        附带 `window_hours`(等到期才判定的窗口)——前端必须把它和口径一起
        展示在界面上,一个不说清楚测量窗口、不说清楚"转化"指什么的转化率
        就是一句空话,店主没法用它做任何判断。
        """
        _require_seller_console()
        from app.config.settings import settings
        stats = get_db().outreach_stats(window_days=window_days)
        stats["window_hours"] = settings.outreach_attribution_window_hours
        return {"success": True, **stats}

    @app.get("/api/admin/growth/followups", dependencies=[Depends(admin_auth)])
    def growth_followups(status: str = "", limit: int = 50):
        """列跟进链(N7:持续沟通=序列自动推进,不是自动发送),供工作台
        「跟进链」小节展示。`status` 留空 = 全部(含已终止的)——已终止的链
        必须带着终止原因一起可见,店主要能看懂"为什么不再跟了";只想看进行中
        的可传 `status=active`。

        `kind_label`/`stop_reason_label` 中文标签同样只在这里附加一次,唯一
        口径分别是 app/agent/tools/growth.py 的 OPPORTUNITY_KINDS 与
        app/multi_agent/followup.py 的 STOP_REASON_LABELS,前端不重抄一份。
        """
        _require_seller_console()
        from app.agent.tools.growth import OPPORTUNITY_KINDS
        from app.multi_agent.followup import STOP_REASON_LABELS

        rows = get_db().list_followups(status=status or None, limit=limit)
        for r in rows:
            kind = r.get("kind") or ""
            r["kind_label"] = OPPORTUNITY_KINDS.get(kind, kind)
            reason = r.get("stop_reason") or ""
            r["stop_reason_label"] = STOP_REASON_LABELS.get(reason, reason)
        return {"success": True, "followups": rows}

    @app.post("/api/admin/growth/drafts/{draft_id}/approve",
              dependencies=[Depends(admin_auth)])
    def approve_draft(draft_id: int):
        """批准并投递触达草稿。**幂等**:并发/连点只有第一次真的发送。

        这是人工闸的核心:批准这个动作本身不可撤销地把消息送到真实买家面前,
        所以谁按下批准、按了几次都不能改变"最终只送一次"的结果——条件更新
        (review_outreach_draft 只对 draft 状态生效)负责认领这一次机会,发送
        失败时把草稿退回 draft 而不是停在 approved,保证坏消息也能重试而不是
        悬空丢失。退回本身也可能失败(数据库异常)——那种情况同样不能悄悄
        冒泡成裸 500,必须如实告知操作者草稿可能悬停在 approved,需人工核查。
        """
        _require_seller_console()
        from app.multi_agent import bus

        db = get_db()
        draft = db.get_outreach_draft(draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")

        # 冲突仲裁:人已经点了批准,但该买家此刻可能正等人工处理——
        # 必须在**认领之前**判,否则会出现"认领成功→仲裁拒绝→退回"的多余翻转,
        # 白白消耗掉这条草稿的一次幂等机会。
        from app.multi_agent.arbitration import check_outreach_allowed
        allowed, arb_code, arb_reason = check_outreach_allowed(
            draft.get("user_id", ""), hitl=hitl)
        if not allowed:
            # block_code 是新增的机器可读字段(BLOCK_MANUAL/BLOCK_OPEN_HANDOFF/
            # BLOCK_UNKNOWN),只在这里追加,不改动既有的 success/sent/reason
            # 三个字段——前端现有解析逻辑不受影响,只是多了一个可选字段。
            return {"success": False, "sent": False, "reason": arb_reason,
                    "block_code": arb_code}

        # 条件更新认领:只有把 draft→approved 改成功的那一次才继续投递
        if not db.review_outreach_draft(draft_id, "approved", reviewed_by="admin"):
            return {"success": True, "sent": False, "reason": "该草稿已被处理过"}

        def _revert_after_failure(base_reason: str, retryable: bool = True) -> dict:
            """投递失败 / 发券失败后统一走的收尾:退回 draft 让店主可以看到并
            处理;绝不停在"已批准但没发/没发券"的悬空态。这一步本身也要能
            失败(revert_outreach_to_pending 是条件更新,rowcount 可能为 0),
            而且它可能直接抛异常(比如数据库这时刚好打不开)——两种情况都
            不能让操作者收到一个没有任何线索的裸 500,那正是这条退回路径
            本应堵住的"悬空 approved"以另一种方式重现。base_reason 是失败
            本身的原因文案(已经带上"投递失败"/"发券失败:…"这类前缀)。

            `retryable`:再点一次批准,这次失败是否真的可能变成成功——文案
            必须如实反映这一点,不能不管三七二十一都说"可重试":
              - 投递失败通常是网络抖动一类的暂时性问题,重试是有意义的
                动作(而且发券这一步在 `issue_for_draft` 里已经能识别出
                "这是同一条草稿的重试",不会因为券已经发过而再次判失败),
                维持"可重试"。
              - 发券失败的两种情形——券码本身不是本店在售的券、这张券
                已经被别的草稿发给了这个买家——都是永久性拒绝:同样的
                草稿再点一次批准,结果不会变,必须明确告知"重试没用,
                需要人工处理",不能留一句会落空的"可重试"。
            """
            try:
                reverted = db.revert_outreach_to_pending(draft_id)
            except Exception:  # noqa: BLE001 退回出错也要报给操作者,不能吞掉
                logger.exception(
                    "%s后退回待审状态出错 draft_id=%s,该草稿可能悬停在 "
                    "approved(已批准但未发送),需人工核查", base_reason, draft_id)
                return {"success": False, "sent": False,
                        "reason": f"{base_reason},且退回待审状态时发生错误;草稿 {draft_id} "
                                  "可能停留在已批准但未发送的状态,请人工核查并处理"}
            if not reverted:
                # 此刻状态理应必是 approved(本请求刚认领的),正常不会走到这里;
                # 但 review_outreach_draft / mark_outreach_sent 的返回值都被认真
                # 对待,这里也不该假装退回一定成功。
                logger.error("%s后退回待审状态未生效(草稿状态异常) draft_id=%s",
                            base_reason, draft_id)
                return {"success": False, "sent": False,
                        "reason": f"{base_reason},且退回待审状态未生效;草稿 {draft_id} "
                                  "状态异常,请人工核查并处理"}
            if retryable:
                return {"success": False, "sent": False,
                        "reason": f"{base_reason},已退回待审,可重试"}
            return {"success": False, "sent": False,
                    "reason": f"{base_reason},已退回待审;直接重试不会成功(原因不会变),"
                              "需人工处理后再批准"}

        # N6:发券碰的是真金白银,而且不可撤销——必须在人工点过批准**之后**、
        # 消息投递**之前**发生。草稿没带 coupon_code 时 issue_for_draft 直接
        # 放行(noop);券码未知(模型编的)或已经被别的草稿发过时判为失败,
        # 与投递失败走同一条退回路径,但文案不同——这两种发券失败都是永久性
        # 拒绝(retryable=False),不能对店主说"可重试"这种会落空的话;而
        # "这条草稿自己上一次已经发过、这次是重试"这种情况,issue_for_draft
        # 内部已经识别出来直接放行(True, ""),根本不会走到这个分支。
        # issue_for_draft 不注册成任何 Agent 工具,只能从这里被调用。
        from app.agent.coupons.grants import issue_for_draft
        coupon_ok, coupon_reason = issue_for_draft(draft, granted_by="admin")
        if not coupon_ok:
            return _revert_after_failure(f"发券失败:{coupon_reason}", retryable=False)

        delivered, deliver_warning = _delivery_result(app.state.deliver_outreach(draft))
        if not delivered:
            return _revert_after_failure("投递失败")

        # 消息已经真实投递给买家(不可撤销)。下游(如营销 Analyst)据此了解
        # 触达已发生,不因"标记已发送"这一步的成败而改变——那只是本地账本。
        marked = db.mark_outreach_sent(draft_id)

        if marked:
            # 记触达归因基线(N3):**只在草稿真的翻成 sent 之后**才记
            # (review finding 4)。此前这段代码在检查 marked 之前就无条件
            # 写基线——如果 mark_outreach_sent 返回 False(今天的状态机下
            # "不可达",但不能假定永远如此),草稿会停在 approved,而
            # `pending_attribution`/`outreach_stats` 只认 status='sent' 的行,
            # 于是这条草稿变成"消息已经真实送到买家面前、却带着一条基线永远
            # 进不了归因队列、也再不会被重新批准"的悬空态——比没有基线更
            # 迷惑,因为它看起来像是正常记过账。把写基线的时机绑定在
            # marked 为真这个前提下,这种"approved + 有基线"的组合从结构上
            # 就不会再出现;marked 为假时改走下面 `if not marked` 分支——
            # 那条分支本来就是"消息已投递但账本没对上"的既有处理方式(如实
            # 告知操作者需要人工核查),不必再发明第二套。
            #
            # status_at_send 取该 order **此刻**的真实状态;无关联订单的商机
            # (弃单/咨询未下单,order_id 恒为空串)写空串,归因侧据此改用"发送后
            # 有没有新建订单"的判法。基线记录失败不该推翻"已经真实发生"的投递,
            # 只记日志(与 touch_conversation 的 fail-soft 同一姿态)。
            try:
                order_id = (draft.get("order_id") or "").strip()
                status_at_send = ""
                if order_id:
                    order = db.get_order(order_id)
                    if order is not None:
                        status_at_send = order.get("status") or ""
                db.set_outreach_baseline(draft_id, status_at_send)
            except Exception:  # noqa: BLE001 基线记录失败不得推翻已发生的投递
                logger.exception("触达基线记录失败(不影响已投递的消息) draft_id=%s",
                                 draft_id)

        bus.publish(bus.EV_OUTREACH_SENT,
                    {"draft_id": draft_id, "user_id": draft.get("user_id")},
                    bus.AGENT_HUMAN, bus.AGENT_ANALYST,
                    correlation_id=draft.get("correlation_id") or None)
        if not marked:
            # 已发送不代表账本也一致:mark_outreach_sent 只对 approved 生效,
            # 若这里返回 False(今天的状态机下不可达,但不能因此就不检查它的
            # 返回值),就不能谎报一次"干净的成功",必须如实告知需要人工核查。
            # 上面已经跳过了写基线,这条草稿不会带着一条看似正常却永远用不上
            # 的基线停在 approved。
            logger.error("投递成功但标记已发送失败(状态非 approved?) draft_id=%s",
                        draft_id)
            reason = (f"消息已投递给买家,但标记为已发送时失败;请人工核查草稿 "
                      f"{draft_id} 的状态")
            if deliver_warning:
                reason = f"{reason};另:{deliver_warning}"
            return {"success": False, "sent": True, "reason": reason}
        if deliver_warning:
            # 消息真的发出去了、账本也对上了,只是投递过程中某个善后动作失败。
            # 这不是失败(绝不能让操作者去重试,那会发第二条),但也不是"干净的
            # 成功",所以照 mark_outreach_sent 那一档的口径:sent=True 如实反映
            # 不可撤销的事实,success=False 提示这里有需要人看一眼的东西。
            return {"success": False, "sent": True, "reason": deliver_warning}
        return {"success": True, "sent": True, "reason": ""}

    @app.post("/api/admin/growth/drafts/{draft_id}/reject",
              dependencies=[Depends(admin_auth)])
    def reject_draft(draft_id: int):
        """驳回草稿:只改状态,绝不投递——与 approve 是两条互斥的出口。"""
        _require_seller_console()
        db = get_db()
        if db.get_outreach_draft(draft_id) is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        ok = db.review_outreach_draft(draft_id, "rejected", reviewed_by="admin")
        return {"success": True, "changed": ok}

    @app.get("/api/admin/collab/timeline", dependencies=[Depends(admin_auth)])
    def collab_timeline(correlation_id: str = "", limit: int = 100):
        """一条协作链的完整时间线:事件 + 该链写下的共享上下文。

        这是"多 Agent 到底协作了什么"唯一可验证的出口——没有它,协作就只是
        一句宣称。复用 `_require_seller_console` 与其它 B 端只读接口同一条
        开关判定,不再为这一个端点单开一份等价逻辑。
        """
        _require_seller_console()
        db = get_db()
        events = db.list_events(correlation_id=correlation_id or None, limit=limit)
        # 按链过滤必须发生在 SQL 里(而不是取回最近 limit 行再在 Python 里筛):
        # shared_context 的行数随会话/异常商品增长,先 LIMIT 后过滤会让稍旧的
        # 协作链读出空的 shared 列表,而那些行明明还在库里。见 list_shared_context。
        shared = db.list_shared_context(limit=limit,
                                        correlation_id=correlation_id or None)
        return {"success": True, "events": events, "shared": shared}

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
