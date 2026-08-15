"""FastAPI 应用工厂：SSE 流式对话 + 会话重置 + 静态前端 + 可观测性看板。"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
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


def _hmdp_token_for(user: str) -> str:
    """demo 模式下把登录用户映射到其 hmdp token(与 /api/chat 同一套映射),
    用于让"我的订单"页/自助下单与 AI 读同一份 hmdp 真实订单。非 demo 返回空。

    **提到模块级的原因**:`_apply_bargain_price` 也要用它判"这个用户的订单会不会
    建到 hmdp"(hmdp 收不了议价价,见那边的说明)。而它原本是 `create_app()` 里的
    闭包——模块级函数引用闭包会在运行时 NameError(第一版就是这么写的,import 能过、
    一调就炸)。抄第二份更糟:"谁走 hmdp"这个判断有两份,迟早分叉成
    "购物车按本地算价、下单却建到了 hmdp"。
    """
    demo_tokens = {str(settings.demo_hmdp_user_id): settings.demo_hmdp_token,
                   "1011": "demo-hmdp-token-1011"}
    return demo_tokens.get(str(user), "") if settings.demo_mode else ""


def _apply_bargain_price(user: str, product: dict, qty: int) -> tuple[float, dict | None]:
    """下单取价:有未兑现的议价成交就用它,否则用标价。返回 `(单价, 成交记录或None)`。

    **这是"议价谈了不算数"那条缺陷的兑现端。** 改造前 `POST /api/order` 对议价的
    引用次数是 0,买家谈到 ¥750(标价 ¥899)下单被收 ¥899——客服刚亲口答应过的价格。

    命名空间的桥:议价按**本地 `products.product_id`**(如 `SHOE-270-BK-42`)记,
    而下单入参是 **hmdp 的 `item_id`**(如 `1`)。`_map_hmdp_product` 现在会把 hmdp
    记录里的 `sku` 带出来,这里就用它做换算——hmdp 老版本没有 `sku` 时取不到成交价,
    按标价走(**宁可多收得对,不可少收得错**)。

    兑现时**重新校验**,不信任存下来的数字:

    - 不低于当前底价:底价可能在成交之后被调过,而这一笔还没兑现;
    - 不高于当前标价:标价降到成交价以下时按标价收——绝不能因为"谈过价"反而收得更贵。

    只取价、**不核销**:核销要等订单真的建出来(见 `_consume_bargain`),否则建单
    失败会白白吃掉买家一次成交。
    """
    list_price = float(product.get("price") or 0)
    sku = product.get("sku")
    if not sku:
        return list_price, None

    # **走 hmdp 建单的用户拿不到议价价——因为我们没法把价格传给 hmdp。**
    #
    # 实测缺陷(拉起 Redis 之后真下了一单才发现):demo 用户的订单建在 hmdp,而
    # `POST {base}/order` 的入参只有 `productId/quantity/address`,**没有价格字段**。
    # 于是:购物车显示「议价 ¥780」、按钮写「去下单 ¥780」、我们的响应也回 780,
    # 而 hmdp 真实建单 ¥899(实测 total=89900 分),议价成交价还被核销掉了。
    # **承诺 780、实收 899、券也没了**——比原来诚实的 899 更糟。
    #
    # 判据放在这个函数里而不是各调用点:购物车、商品详情、下单三处都调它,
    # 一处判、三处一致。分散判早晚出现"购物车显示 780、结账收 899"。
    #
    # 这是 demo 模式专属的缺口:真实部署 demo_mode=False,所有订单走本地库,
    # 议价正常生效。要让它在 hmdp 上也生效,得 hmdp 支持接收成交价——那是另一个
    # 系统的改动,不在这里假装能做到。
    if _hmdp_token_for(user):
        return list_price, None

    try:
        deal = get_db().active_bargain_deal(user, str(sku))
    except Exception:  # noqa: BLE001 取不到成交价就按标价走,不能让下单因此失败
        logger.warning("查询议价成交失败,本单按标价结算 (user=%s sku=%s)", user, sku,
                       exc_info=True)
        return list_price, None
    if not deal:
        return list_price, None

    price = float(deal["price"])
    try:
        local = get_db().get_product(str(sku)) or {}
        floor = local.get("floor_price")
        if floor is not None:
            price = max(price, float(floor))
    except Exception:  # noqa: BLE001 校不到底价时不放宽,保持成交价
        pass
    price = min(price, list_price)
    return round(price, 2), deal


def _consume_bargain(deal: dict | None, order_id: str) -> None:
    """订单建成之后核销这笔成交(一次性)。

    条件更新失败(并发下另一单先兑走了)**只记日志不报错**:订单已经建出来了,
    此时把接口回成失败会让买家看到"下单失败"而库里其实有单——那比少收一次价更糟。
    """
    if not deal:
        return
    try:
        if not get_db().consume_bargain_deal(int(deal["id"]), order_id):
            logger.warning("议价成交核销未生效(可能已被另一单兑走) deal_id=%s order=%s",
                           deal.get("id"), order_id)
    except Exception:  # noqa: BLE001 核销失败不能回滚一笔已经建好的订单
        logger.warning("议价成交核销失败 deal_id=%s order=%s", deal.get("id"), order_id,
                       exc_info=True)


def _order_result_for(order_id: str | None, user: str) -> dict:
    """幂等键命中时,拼出与第一次下单**同形状**的响应。

    重试的语义是"我不知道上次成不成功,请给我结果"——所以返回 200 + 原订单,
    不是 409。客户端拿到的字段必须与第一次一致,否则它会因为形状不同而走进错误分支,
    等于幂等只做了一半。

    订单可能在本地库、也可能在 hmdp(`POST /api/order` 按 token 分两条路建单),
    所以这里先查本地,查不到就按 hmdp 那条路的形状返回——**不为了补齐字段再去请求
    一次 hmdp**:那会让一次重试变成一次外部调用,而重试往往正发生在网络不稳的时候。
    `total` 拿不到时给 None 而不是 0:0 是个会被前端当真的金额。
    """
    label, total = "待支付", None
    if order_id:
        try:
            row = get_db().get_order(order_id)
            if row is not None and row.get("user") == user:
                label = STATUS_LABELS.get(row["status"], row["status"])
                total = row.get("total")
        except Exception:  # noqa: BLE001 查不到就按 hmdp 形状返回,不让重试因此失败
            pass
    return {"success": True, "order_id": order_id, "status_label": label,
            "total": total, "idempotent_replay": True}


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


#: 人工接管期间给买家的短路提示。具名成常量是因为它现在有**两个**用处:一处是这一轮
#: 回给买家的文本,一处是写进会话历史的那条 assistant 消息(见接管那道门)。两处各写一遍
#: 字面量,改文案时漏掉一处,买家看到的和历史里记下的就会对不上——而那种不一致在复盘
#: 时最难解释:坐席会以为系统当时说了另一句话。
MANUAL_TAKEOVER_NOTICE = "🎧 当前会话已转由人工客服处理，请稍候…"


class ProductServiceUnavailable(RuntimeError):
    """商品服务(hmdp)连不上——**不等于商品不存在**。

    两者在界面上是完全不同的两句话:"这个商品下架了"会让买家去找别的商品甚至离开,
    而"服务暂时不可用,请稍后再试"会让他等一会儿再来。把故障说成下架,等于用一次网络
    抖动赶走一个正要下单的买家。

    做成异常而不是多一种返回值:调用方现在都写着 `if not p`,新增一种假值会被悄悄
    当成"不存在"——那正是要修的这个错本身。
    """


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

    # W1 服务化 L2:后台预热进程级构造成本(MCP 连接/LLM 传输层/FAQ 缓存/本地
    # 知识库索引),不让"第一个真实用户"单独承担这笔冷启动开销。与上面的
    # reaper 同一个判断:只在生产路径(未注入 manager)启动,测试注入
    # session_manager 时不会多起这个后台线程。挂到 app.state 供测试内省
    # (断言线程已启动/存活),create_app() 本身不等它跑完——见
    # app/api/warmup.py 顶部"绝不阻塞服务就绪"的铁律。
    app.state.warmup_thread = None
    if session_manager is None and settings.startup_warmup_enabled:
        from app.api.warmup import warm_process_in_background
        app.state.warmup_thread = warm_process_in_background()

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

    def _gate_reply_stream(gate: str, text: str, session_id: str = "", user_id: str = "",
                           user_message: str = "", conversation: Optional[tuple] = None):
        """限流/人工接管/规则快路径/成本上限 命中时的短路回复(阶段一 gap④)。

        这四道门都在 `run_agent_streaming`(也就是 `langfuse_turn` 建根 trace 的
        地方)之前就 `return _reply_stream(...)`,此前这些轮次在 Langfuse 里
        完全不存在——而它们恰恰是**最快**的那批轮次,缺席会让延迟统计系统性
        偏高。这里补一条极轻量的命名 trace,只记"哪道门命中的"+ 这道门自身的
        判定/落盘耗时,不去伪造一段它并未经历的模型调用时长。

        `background_trace` 已经是门控关/未装/异常即返回 None 的 best-effort
        实现,这里只是再包一层 try 防 `root.update` 本身抛错——四道门本身的
        放行/拒绝逻辑完全不依赖这条 trace 是否记成功。
        """
        from app.observability.langfuse_bridge import background_trace
        with background_trace(f"gate:{gate}", session_id=session_id or None,
                              user_id=user_id or None, input=user_message) as root:
            if root is not None:
                try:
                    root.update(output=text, metadata={"gate": gate})
                except Exception:  # noqa: BLE001 记录失败不能反过来影响这轮短路回复
                    pass
        return _reply_stream(text, conversation=conversation)

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
        out = {
            "id": str(p.get("id")), "title": p.get("title"),
            "price": round((p.get("price") or 0) / 100, 2),
            "stock": p.get("stock"), "image": img,
            "description": p.get("description") or "",
        }
        # sku:**两套商品标识之间的映射一直就在 hmdp 里,只是被这个映射函数丢在了
        # 边界上。** hmdp 的商品记录同时带 `id`(ecom 下单用的 item_id)与 `sku`
        # (本地 products.product_id,也是议价与经营分析用的那个),实测
        # `{"id": 1, ..., "sku": "SHOE-270-BK-42"}`。
        #
        # 这一条影响两件已知的事:①议价谈成的价格作用不到订单,原因之一就是"议价用
        # 本地 product_id、下单用 hmdp item_id,两者对不上"——而对照关系其实现成;
        # ②`product_ref.py` 记的命名空间问题同源。所以带上它不是"顺手多给个字段",
        # 是把一个被丢掉的既有映射接回来。
        #
        # 用 setdefault 语义(仅在 hmdp 真给了值时才带):hmdp 老版本可能没有这个字段,
        # 缺失时不塞空串——空 sku 会被下游当成"有 sku 但是空的",比没有更糟。
        if p.get("sku"):
            out["sku"] = str(p["sku"])
        # floorPrice 同理:议价底价在 hmdp 侧也存着(实测 floorPrice=75000 分),
        # 而本地 products.floor_price 是另一份。带上来让"底价到底以谁为准"这个问题
        # 至少变得可见——不带的话下游只能假设本地那份是唯一来源。
        if p.get("floorPrice") is not None:
            out["floor_price"] = round((p.get("floorPrice") or 0) / 100, 2)
        return out

    @app.get("/api/products")
    def products(keyword: str = ""):
        """商城:代理 hmdp 商品列表(公开),供前端渲染商品卡。

        **降级必须可区分**。改造前失败时 `return {"products": []}`,前端因此
        无法分辨"这家店真的没有商品"和"商品服务连不上"——页面显示"0 件商品",
        没有报错、没有日志。买家看到一个空店铺,运维看到一切正常。

        现在把 `degraded` 一并下发:空列表 + degraded=true 是故障,空列表 +
        degraded=false 才是真的没商品。这是同一个响应体里两件完全不同的事,
        不能共用一种表示。
        """
        from app.net.internal_http import internal_client, warn_if_proxy_would_break

        base = settings.hmdp_base_url.rstrip("/")
        url = f"{base}/product/list"
        try:
            with internal_client(url, timeout=3.0) as c:
                r = c.get(url, params={"keyword": keyword})
                data = r.json() if r.status_code == 200 else {}
            if r.status_code != 200:
                logger.warning("hmdp 商品列表 http %s: %s", r.status_code, r.text[:200])
                return {"products": [], "degraded": True,
                        "reason": f"商品服务返回 {r.status_code}"}
            items = (data.get("data") or []) if data.get("success") else []
            return {"products": [_map_hmdp_product(p) for p in items], "degraded": False}
        except Exception as exc:  # noqa: BLE001 商城页降级不该 500
            logger.warning("hmdp 商品列表读取失败 url=%s: %s", url, exc)
            warn_if_proxy_would_break(url)
            return {"products": [], "degraded": True,
                    "reason": f"商品服务连不上({type(exc).__name__})"}

    def _fetch_hmdp_product(item_id: str) -> Optional[dict]:
        """按 id 从 hmdp 取单个商品并映射为前端结构。

        **"商品不存在"返回 None,"商品服务连不上"抛 `ProductServiceUnavailable`。**
        两者必须分开——这不是新主张,紧挨着的 `/api/products` 有一整段 docstring 讲
        同一条纪律("买家看到一个空店铺,运维看到一切正常"),而这里原本的注释也已经
        点明了后果:"一次代理/网络故障会在界面上呈现为『这个商品下架了』"。

        原来选的缓解是"失败必须留日志"。**日志不是给买家的出口**:买家看到的仍然是
        下架,而 `create_order` 更把它翻成 `404 商品不存在或已下架`——网络抖一下就告诉
        想下单的买家这件商品没了。实测撞到:hmdp 挂掉时 `/api/product/1` 返回
        `{"product": null}`,不带任何降级标记,而同一时刻 `/api/products` 老老实实报了
        `degraded: true`。同一个故障,两个相邻端点两种说法。

        抛异常而不是多加一种返回值:调用方现在都写着 `if not p`,新增一种假值会被
        悄悄当成"不存在"——那正是要修的这个错本身。
        """
        if not item_id.isdigit():
            return None
        from app.net.internal_http import internal_client, warn_if_proxy_would_break

        base = settings.hmdp_base_url.rstrip("/")
        url = f"{base}/product/{item_id}"
        try:
            with internal_client(url, timeout=3.0) as c:
                r = c.get(url)
                data = r.json() if r.status_code == 200 else {}
            p = data.get("data") if data.get("success") else None
            return _map_hmdp_product(p) if p else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("hmdp 商品详情读取失败 item=%s: %s", item_id, exc)
            warn_if_proxy_would_break(url)
            raise ProductServiceUnavailable(str(exc)) from exc

    @app.get("/api/product/{item_id}")
    def product_detail(item_id: str, request: Request):
        """按 id 取单个商品(结构化),供聊天窗内商品卡渲染。

        与 `/api/products` **同一条口径**:`product=null + degraded=false` 是"没有这个
        商品",`product=null + degraded=true` 是"商品服务连不上"。前端据此决定显示
        "商品不存在"还是"服务暂时不可用,稍后再试"——把故障说成下架,等于用一次网络
        抖动赶走一个正要下单的买家。
        """
        try:
            p = _fetch_hmdp_product(item_id)
            if p:
                # 与购物车同一条口径:买家看到的价必须等于他会被收的价。
                # 这个端点本身是公开的(不要求登录),`_resolve_user` 在未登录/
                # auth 关闭时回落成默认用户,取不到成交价就照常显示标价——
                # 不因为"想显示议价"而给这个端点加登录要求。
                unit, deal = _apply_bargain_price(_resolve_user(request, None), p, 1)
                p = {**p, "deal_price": unit if (deal and unit < (p.get("price") or 0)) else None}
            return {"product": p, "degraded": False}
        except ProductServiceUnavailable as exc:
            return {"product": None, "degraded": True,
                    "reason": f"商品服务连不上({type(exc.__cause__).__name__ if exc.__cause__ else 'Error'})"}

    def _hmdp_token_for_user(user: str) -> str:
        """见模块级的 `_hmdp_token_for`。保留这个名字是为了不动几十处调用点。"""
        return _hmdp_token_for(user)

    def _fetch_hmdp_order(order_id: str, token: str) -> Optional[dict]:
        """按单号从 hmdp 取一笔订单(已映射)。失败/无返回 None。

        支付成功后用它回读真实状态,而不是在前端硬编码「付完就是待发货」——
        状态归属上游,前端猜出来的那个值迟早和真相分叉。
        """
        from mcp_server.hmdp_mapping import map_order
        from app.net.internal_http import internal_client

        base = settings.hmdp_base_url.rstrip("/")
        url = f"{base}/order/{order_id}"
        try:
            with internal_client(url, timeout=4.0) as c:
                r = c.get(url, headers={"authorization": token})
                d = r.json() if r.status_code == 200 else {}
        except Exception as exc:  # noqa: BLE001 回读失败不该把一次成功的支付变成报错
            logger.warning("hmdp 回读订单失败 order=%s: %s", order_id, exc)
            return None
        return map_order(d["data"]) if d.get("success") and d.get("data") else None

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
        from app.net.internal_http import internal_client

        base = settings.hmdp_base_url.rstrip("/")
        url = f"{base}/order/of/me"
        with internal_client(url, timeout=4.0) as c:
            r = c.get(url, headers={"authorization": token})
            d = r.json() if r.status_code == 200 else {}
        raw = (d.get("data") or []) if d.get("success", False) else []
        orders = [_fmt_order(map_order(o)) for o in raw]
        orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)
        return orders

    @app.post("/api/order")
    def create_order(req: CreateOrderRequest, request: Request,
                     idempotency_key: str = Header("", alias="Idempotency-Key")):
        """自助下单:用户在商城/商品卡点『立即购买』(或购物车「去下单」)→ 按登录身份建单。
        demo 用户 → 建到 hmdp(hmdp 自身已有真实支付流程,不受本开关影响,状态
        恒为待支付);其它 → agent 订单库,初始状态由 initial_order_status() 按
        settings.unpaid_flow_enabled 决定(开=unpaid 需再付款,关=pending,与
        改造前逐字节一致)。下单成功后把该商品在本地购物车里的 active 行标记为
        converted——不论这次下单是从购物车「去下单」发起,还是商品卡「立即购买」
        绕开购物车直接下的单,买家事实上都已经为这件商品完成了下单。"""
        user = _resolve_user(request, None)

        # 幂等键(业界惯例的 `Idempotency-Key` 头,Stripe/Square 同名)。**不传就是
        # 改造前的行为**,老客户端不会被打断。
        #
        # 为什么需要:前端的在途 ref 只挡住"买家手抖点两下"(见 ㊿③),挡不住网络
        # 超时后的重试、多标签页、脚本重放——那些都会各建一笔真订单。
        #
        # 三态由 `claim_idempotency_key` 用 UNIQUE(user,key) 判,不是"先查再插":
        # 后者在并发下两个请求会同时查到"没有"然后各建一笔(本仓库反复修过的形状)。
        key = (idempotency_key or "").strip()[:200]
        if key:
            state, existing = get_db().claim_idempotency_key(user, key)
            if state == "done":
                # 重试的语义是"我不知道上次成不成功,请给我结果"——所以返回 200 +
                # 原订单,不是 409。客户端拿到的与第一次完全一样。
                return _order_result_for(existing, user)
            if state == "in_flight":
                # 真正的并发:另一个请求正拿着这个键建单。**绝不能自己再建一笔。**
                # 409 让客户端稍后重试,那时它会拿到 "done" 分支的原订单。
                raise HTTPException(409, "同一下单请求正在处理中,请稍后重试")

        try:
            p = _fetch_hmdp_product(req.item_id)
        except ProductServiceUnavailable:
            # **失败必须可重试。** 不释放占位行的话,这个键会永久钉在 in_flight 上,
            # 买家点重试拿到的永远是"正在处理中",而实际上一笔单都没建。
            if key:
                get_db().release_idempotency_key(user, key)
            # **不能说"已下架"。** 这是买家点了「立即购买」之后看到的那句话:把一次
            # 网络故障说成下架,等于劝退一个正要付钱的人,而且他不会再回来试。
            # 503 = 暂时不可用、值得重试;404 = 这件商品没了,两者的买家行为完全不同。
            raise HTTPException(503, "商品服务暂时不可用，请稍后再试")
        if not p:
            raise HTTPException(404, "商品不存在或已下架")
        qty = max(1, min(int(req.quantity or 1), 99))
        unit_price, bargain = _apply_bargain_price(user, p, qty)
        total = round(unit_price * qty, 2)
        token = _hmdp_token_for_user(user)
        if token:
            from app.net.internal_http import internal_client

            base = settings.hmdp_base_url.rstrip("/")
            pid = int(req.item_id) if req.item_id.isdigit() else req.item_id
            url = f"{base}/order"
            import httpx as _httpx

            try:
                with internal_client(url, timeout=4.0) as c:
                    r = c.post(url,
                               json={"productId": pid, "quantity": qty,
                                     "address": req.shipping_address or "上海市浦东新区示例路 1 号"},
                               headers={"authorization": token})
                    d = r.json() if r.status_code == 200 else {}
            except _httpx.TimeoutException:
                # **超时与连接失败必须分开处理,因为"订单建没建"的答案不一样。**
                #
                # 实测(前端体验时踩到):Redis 挂掉 → hmdp 的下单接口挂住(它要
                # Redis 做库存)→ 这里 4 秒读超时 → 异常没人接 → 买家看到一个裸的
                # 500 Internal Server Error。而 hmdp 的只读接口当时是好的
                # (GET /product/1 → 200),所以商城看着一切正常,只有下单会炸。
                #
                # 超时的含义是**我们不知道对面做了什么**:请求可能已经到达并建了单,
                # 也可能没有。所以:
                #   ① 不释放幂等键——释放了,买家拿同一个 key 重试就会建出第二笔;
                #   ② 不核销议价成交价——那笔单是否存在还不确定;
                #   ③ 文案让买家**去看订单**,而不是"请重试"。劝一个可能已经下过单的
                #      人再下一次,是这里最坏的建议。
                logger.warning("hmdp 下单超时,结果未知 user=%s item=%s", user, req.item_id)
                raise HTTPException(
                    503, "下单请求已发出但未收到确认,请稍后在「我的订单」查看是否已生成;"
                         "为避免重复下单,请不要立即重试。")
            except _httpx.RequestError as exc:
                # 连接层面的失败(拒绝/DNS/断开)= **请求没送达**,这个是确定的。
                # 所以可以安全地释放幂等键让买家重试,议价也没被消耗。
                if key:
                    get_db().release_idempotency_key(user, key)
                logger.warning("hmdp 下单连接失败 user=%s item=%s: %s",
                               user, req.item_id, type(exc).__name__)
                raise HTTPException(503, "下单服务暂时不可用,请稍后再试。")
            if not d.get("success"):
                raise HTTPException(502, d.get("errorMsg") or "下单失败,请稍后再试")
            try:
                get_db().mark_cart_converted(user, [req.item_id])
            except Exception:  # noqa: BLE001 购物车状态清理失败不影响已下单结果
                pass
            oid = d.get("data")
            _consume_bargain(bargain, str(oid))
            if key:
                get_db().finish_idempotency_key(user, key, str(oid))
            return {"success": True, "order_id": oid, "status_label": "待支付", "total": total}
        order = get_db().create_order(
            user=user,
            items=[{"name": p["title"], "sku": f"HMDP-{req.item_id}", "quantity": qty,
                    "price": unit_price}],
            total=total, status=initial_order_status(), shipping_address=req.shipping_address,
        )
        _consume_bargain(bargain, order["order_id"])
        try:
            get_db().mark_cart_converted(user, [req.item_id])
        except Exception:  # noqa: BLE001 购物车状态清理失败不影响已下单结果
            pass
        if key:
            get_db().finish_idempotency_key(user, key, order["order_id"])
        return {"success": True, "order_id": order["order_id"],
                "status_label": STATUS_LABELS.get(order["status"], order["status"]),
                "total": order["total"]}

    @app.post("/api/order/{order_id}/pay")
    def pay_order_endpoint(order_id: str, request: Request):
        """买家为自己的未支付订单完成支付(unpaid → pending)。这是**买家自己**
        的动作,不是 Agent 工具——与"不代客下单"同一条底线,故只做端点。归属
        与幂等都下沉到 Database.pay_order 的条件更新里:付别人的单、或对已经
        支付过的订单重复调用,都返回 False,不改变订单状态。

        **必须与下单/列表走同一条路由规则。** 改造前这里只改本地库,而
        `POST /api/order` 对 demo 用户是把订单建到 hmdp 的、`GET /api/orders`
        也是从 hmdp 读的——于是买家看到自己刚下的单、点「去支付」,拿到的却是
        「订单不存在、不属于当前用户,或已完成支付」:订单在 hmdp 里,本地库
        根本没有这一行。写入走一条路径、状态变更走另一条,是与 MCP 那条
        (AI 读本地库、页面读 hmdp)完全相同的形态。
        """
        user = _resolve_user(request, None)

        # **按"这笔订单在哪"路由,不按"这个用户有没有 token"。**
        #
        # 上面那段说明记录了修过的一个反向问题:支付只改本地库、而订单建在 hmdp,
        # 买家点「去支付」得到"订单不存在"。当时的修法是"支付跟着下单的路由规则走"
        # ——但那条规则是按**用户**判的,它隐含假设"有 token 的买家的订单一定在
        # hmdp"。这个假设不成立:本地库里确实存着 user='1' 的订单(实测三笔),而
        # `unpaid_flow_enabled=True` 时本地单起始状态就是 unpaid。于是同一个 bug 被
        # 镜像了一次——**hmdp 支付 + 本地订单**,买家同样永远付不了款,拿到的是
        # "订单不存在、不属于当前用户,或已完成支付"(hmdp 活着)或"支付服务暂时
        # 不可用"(hmdp 挂了)。实测走过这两种。
        #
        # 按订单位置判从根上避免这一类:本地有这一行就本地付(归属与幂等仍由
        # `pay_order` 的条件更新兜住),本地没有才去 hmdp 找。这条规则不依赖
        # "谁有 token",所以不会因为路由规则再改一次而重新失配。
        local_order = None
        try:
            local_order = get_db().get_order(order_id)
        except Exception:  # noqa: BLE001 读不到就按"不在本地"处理,交给 hmdp 那条路
            local_order = None

        token = "" if local_order is not None else _hmdp_token_for_user(user)
        if token:
            from app.net.internal_http import internal_client

            base = settings.hmdp_base_url.rstrip("/")
            url = f"{base}/order/{order_id}/pay"
            try:
                with internal_client(url, timeout=4.0) as c:
                    r = c.post(url, headers={"authorization": token})
                    d = r.json() if r.status_code == 200 else {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("hmdp 支付失败 order=%s: %s", order_id, exc)
                raise HTTPException(502, "支付服务暂时不可用，请稍后再试") from exc
            if not d.get("success"):
                # 上游的拒绝原因(已支付/不属于你/不存在)原样透出,不改写成一句
                # 含糊的兜底——买家需要知道到底是哪一种。
                raise HTTPException(400, d.get("errorMsg") or "支付失败，请稍后再试")
            fresh = _fetch_hmdp_order(order_id, token)
            status = (fresh or {}).get("status") or "pending"
            return {"success": True, "order_id": order_id, "status": status,
                    "status_label": STATUS_LABELS.get(status, status)}

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
        """当前用户的购物车(供「购物车」页),**带商品信息**。

        改造前只返回购物车行本身(sku/quantity/status),不带商品名与价格,
        于是购物车页面上一件商品只显示一个原始 sku(如「1」)。后果不只是难看:
        页面上那个「去下单」按钮会**在买家从未看到价格的情况下提交订单**,
        下完单才用弹窗告诉他付了多少——这是让人闭着眼睛付钱。

        商品信息在**后端**补,不让前端去拉商品列表自己 join:购物车里的价格
        必须与商城页、与最终下单金额同源,前端各自查一次迟早对不上。

        单个商品查不到时保留该行并标 `product_missing`(而不是丢掉或填 0):
        商品下架、商品服务抖动都可能查不到,而买家的购物车行是他自己加的,
        不该因为一次查询失败就从界面上消失。
        """
        user = _resolve_user(request, None)
        rows = get_db().list_cart(user)
        out = []
        degraded = False
        for row in rows:
            item = dict(row)
            try:
                p = _fetch_hmdp_product(str(item.get("sku") or ""))
            except ProductServiceUnavailable:
                # 这一行的**行为**与"商品下架"一致(保留行、不显示价格),所以仍走
                # product_missing;但整份响应要带 degraded——否则买家看到满车商品
                # 全是"信息缺失",会以为自己加的东西都下架了。逐行标注也可以,
                # 但一次故障通常是整批取不到,响应级一个标记更贴合实际、也更简单。
                p = None
                degraded = True
            if p:
                # **买家看到的价必须等于他会被收的价。**
                #
                # 实测缺陷(前端体验时抓到):买家谈成 ¥780 后,购物车仍显示单价 ¥899、
                # 按钮写「去下单 ¥899」,而下单实收 ¥780(`_apply_bargain_price`)。
                # 三重后果:①他不知道议价生效了,整个功能对他不可见;②可能因为
                # "看着还是原价"就不下单,议价白谈;③**按钮上的金额是承诺**,
                # 而实收是另一个数——这个项目修过"让人闭着眼睛付钱",同一条纪律。
                #
                # 取价复用下单那条路的 `_apply_bargain_price`,不另写一套:两处若
                # 各算各的,迟早出现"购物车显示 780、结账收 899"这种更糟的形态。
                qty_ = item.get("quantity") or 0
                unit, deal = _apply_bargain_price(user, p, qty_)
                item.update({
                    "title": p.get("title"), "price": p.get("price"),
                    "image": p.get("image"), "stock": p.get("stock"),
                    # deal_price 只在**真的更便宜**时出现:等于标价时给它反而会让
                    # 前端显示一条"划掉 899 → 899"的假优惠。
                    "deal_price": unit if (deal and unit < (p.get("price") or 0)) else None,
                    "subtotal": round(unit * qty_, 2),
                    "product_missing": False,
                })
            else:
                # 价格给 None 而不是 0:0 会被前端渲染成「¥0」,让买家以为免费。
                item.update({"title": None, "price": None, "image": None,
                             "stock": None, "subtotal": None, "product_missing": True})
            out.append(item)
        # degraded=true 时 product_missing 的含义从"这些商品没了"变成"这一刻取不到"
        # ——同一个字段,两种截然不同的处置,必须让前端分得出来。
        return {"success": True, "items": out, "degraded": degraded}

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
        """当前买家可评价的已签收订单项。

        **必须与订单列表同源。** 改造前只查本地订单表,而 demo 用户的订单全在
        hmdp——于是 `reviewable_items` 永远返回空,**整个评价功能对默认模式的
        买家不可达**(而 hmdp 的状态机 unpaid→pending→shipped→delivered 明明
        能走到已签收)。这是与"支付只改本地库"完全相同的形态:同一份业务对象,
        读一条路径、判定另一条路径。

        评价本身仍留在 agent 侧,这是刻意的:hmdp 没有评价这个概念(只有 blog
        评论)。所以正确的组合是"订单从买家实际看到的那份取,已评记录从本地取"。
        """
        uid = _resolve_user(request, None)
        token = _hmdp_token_for_user(uid)
        if not token:
            return {"success": True, "items": get_db().reviewable_items(uid)}

        try:
            orders = _hmdp_my_orders(token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读 hmdp 订单以判可评价失败 user=%s: %s", uid, exc)
            # 读不到就回落本地(可能为空),而不是 500——评价入口消失比整页报错轻。
            return {"success": True, "items": get_db().reviewable_items(uid),
                    "degraded": "订单服务不可用，可评价列表可能不完整"}

        reviewed = get_db().reviewed_pairs(uid)
        items = [
            {"order_id": o["order_id"], "sku": it.get("sku"),
             "name": it.get("name"), "delivered_at": o.get("created_at")}
            for o in orders if o.get("status") == "delivered"
            for it in (o.get("items") or [])
            if (o["order_id"], it.get("sku")) not in reviewed
        ]
        return {"success": True, "items": items}

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
            return _gate_reply_stream("rate_limit", "⏳ 您发送得太快啦，请稍后再试～",
                                      session_id=req.session_id, user_id=req.user_id,
                                      user_message=req.message)

        # 2) 人工接管中：短路，不调用 Agent。同理用原始 ID:坐席是对客户端
        #    正在用的会话 ID 做接管;先换发会让接管被静默绕过。
        if hitl is not None and hitl.manual_mode.is_manual(req.session_id):
            # 这一轮也要入历史并落盘。**这条纪律隔壁的快路径早就写着**(见下面第 4 道
            # 门的注释:"仍把这轮问答写进会话历史并落盘,保证刷新/切换后可回显"),
            # 只有接管这条路漏了。
            #
            # 实测后果:接管期间买家说的话完全不进历史——买家刷新页面自己刚说的话
            # 不见了,而更疼的是**坐席在工作台打开这个会话,看不到买家在等待期间说了
            # 什么**,而接管正是为了处理买家在说的事。审计与复盘同样缺这一段。
            #
            # 只在会话已存在时追加:这道门刻意跑在 ensure_active **之前**(注释见上),
            # 所以这里不能建会话,只能往已有的那个上追加。取不到就跳过持久化,绝不能
            # 因为记历史失败而让接管这道门本身失效。
            _mt_agent = None
            try:
                if get_db().get_conversation(req.session_id) is not None:
                    _mt_agent = manager.get_or_create(req.session_id, req.user_id)
            except Exception:  # noqa: BLE001 取不到就不记,接管照常生效
                _mt_agent = None
            if _mt_agent is not None:
                # 与快路径同样先抢会话锁再改 raw_messages:并发改同一个列表会串。
                with session_lock.guard(req.session_id) as _got:
                    if _got:
                        _msgs = getattr(_mt_agent, "raw_messages", None)
                        if isinstance(_msgs, list):
                            _msgs.append({"role": "user", "content": req.message})
                            _msgs.append({"role": "assistant", "content": json.dumps({
                                "intent": "manual_takeover", "confidence": 1.0,
                                "reply": MANUAL_TAKEOVER_NOTICE,
                                "requires_human": True, "follow_up_question": None,
                            }, ensure_ascii=False)})
                            _save = getattr(_mt_agent, "save", None)
                            if callable(_save):
                                try:
                                    _save()
                                except Exception:  # noqa: BLE001 保存失败不影响本轮回复
                                    pass
            return _gate_reply_stream("manual_takeover", MANUAL_TAKEOVER_NOTICE,
                                      session_id=req.session_id, user_id=req.user_id,
                                      user_message=req.message)

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
                        return _gate_reply_stream(
                            "fast_path", "⏳ 您的上一条消息还在处理中，请稍候再发～",
                            session_id=req.session_id, user_id=req.user_id,
                            user_message=req.message, conversation=(active_id, rotated))
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
                return _gate_reply_stream("fast_path", fp["reply"],
                                          session_id=req.session_id, user_id=req.user_id,
                                          user_message=req.message,
                                          conversation=(active_id, rotated))

        # 5) 成本上限（防烧爆 API Key）
        if not cost_guard.allow():
            return _gate_reply_stream("cost_ceiling", "🛑 今日服务已达使用上限，请明天再来～",
                                      session_id=req.session_id, user_id=req.user_id,
                                      user_message=req.message)

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
        with session_lock.guard(session_id) as got:
            if not got:
                # 抢不到锁 = 有一轮 /api/chat 正在改 raw_messages。此时照样快照,
                # 拿到的是**撕裂的对话**(例如只有用户那句、没有对应的助手回复,
                # 或工具序列写了一半),而这份快照会直接被蒸馏成长期记忆事实——
                # 一条错的长期记忆会跨会话反复影响后续回答,比这次巩固失败糟得多。
                #
                # 与 /api/chat 的处理方式不同是刻意的:那边面对的是等着回复的买家,
                # 必须给一句话;巩固是可重试的显式动作,如实说"忙,稍后再试"即可。
                # 全项目 5 处 guard 里,此前只有这一处没检查 got。
                return {"enabled": True, "busy": True, "count": 0, "facts": [],
                        "reason": "该会话正在处理上一条消息，请稍后再巩固"}
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
    def metrics(window_hours: float = 0):
        """看板指标。`window_hours=0`(默认)= 全部历史,与改造前一致。

        加窗口是因为"全部历史"这一种口径会让**已经修好的问题永远显示为红色**:
        修完之后新调用全成功,而累计值被几百条旧失败压着,红色要好几周才褪。
        运维看到的是"改了没用",实际是"口径不对"。
        """
        return compute_metrics(store, window_hours=window_hours or None)

    @app.get("/api/traces", dependencies=[Depends(admin_auth)])
    def traces(limit: int = 20, session_id: Optional[str] = None,
               window_hours: float = 0):
        """最近的 trace 列表。

        `window_hours` 与 `/api/metrics` 同名同义(0 = 全部历史,保持改造前的默认),
        这样看板上的卡片与「最近请求」表格才是同一个口径——两者不一致时,页面会
        一边写着"统计口径:近 1 小时 / 总请求数 1",一边列出 50 行跨 40 小时的记录
        (走查实测,见 store.recent_traces 的说明)。
        """
        import time as _time
        since = (_time.time() - window_hours * 3600) if window_hours else None
        return store.recent_traces(limit=limit, session_id=session_id, since=since)

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

        # get_catalog_all:管理页要列出仓库里**真实存在的每一份** skill 并显示归属。
        # 这里没有 actor 上下文(HTTP 请求,不是买家/店主的某一轮对话),走
        # get_catalog() 会退化成"只看买家",卖家侧 skill 在页面上凭空消失。
        live = SkillManager(skills_dir=settings.skills_dir, enabled=True).get_catalog_all()

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

                # 门禁就绪度:**在操作者点「转正」之前**就说清门禁评不评得了。
                #
                # 实测走完一遍才发现这件事有多绕:点「转正」→ 400「未跑评测门禁」→
                # 勾上跑门禁再点 → 400「该技能没有门禁用例」。两次失败之后才知道
                # 这个候选根本不可能通过这个按钮上线,而这个事实**在页面加载时就是
                # 已知的**(只要数一下评测集)。数用例不花钱、不调 LLM,没有理由
                # 让操作者用两次 400 去把它试出来。
                try:
                    from app.agent.skills.gate import gate_readiness
                    r = gate_readiness(item["name"], settings.eval_dataset_path)
                    item["gate_cases"] = r["count"]
                    item["gate_evaluable"] = r["evaluable"]
                    item["gate_underpowered"] = r["underpowered"]
                    item["gate_note"] = r["note"]
                    # 人工/合成分开给前端。**不能只给总数**:5 条全自动合成的
                    # "门禁用例 5 条",与 5 条人工用例在证据强度上完全不是一回事,
                    # 而界面上长得一模一样正是自动化最容易骗到人的地方。
                    item["gate_human_cases"] = r["human_count"]
                    item["gate_synthetic_cases"] = r["synthetic_count"]
                except Exception:  # noqa: BLE001 数不出来就不显示,不拖累整份列表
                    item["gate_cases"] = None
                    item["gate_evaluable"] = None
                    item["gate_underpowered"] = None
                    item["gate_human_cases"] = None
                    item["gate_synthetic_cases"] = None
                    item["gate_note"] = "门禁用例数未知"
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

        # 失败归因:**这个 skill 的失败里,有多少根本不该算在它头上。**
        #
        # 只看上面那张 outcome 表会得出完全错误的结论:实测 track-order 的
        # success 15 / tool_error 38 读起来是"成功率 28%,这个 skill 很烂",
        # 而 41 条失败里 37 条是**归属校验正确地拦住了跨用户访问**——那是安全
        # 机制在按设计工作,不是 skill 缺知识。少了这一层,运营看着这张表会去
        # 改一份一个字都没写错的流程文档。
        #
        # 纯确定性计算(读库判归属,不调 LLM),failure_attribution 归因不到的
        # 那一类如实报成 undetermined,绝不为了让表格好看而塞进某一类。
        failure_attribution: dict[str, dict[str, int]] = {}
        try:
            from app.agent.skills.attribution import partition

            db = get_db()
            # **单独按 outcome 查,不复用上面那个窗口。** 那个窗口是所有 outcome
            # 共用的:失败远少于成功,一段正常运行就能把窗口填满,失败被整体挤出去
            # ——实测最近 200 条轨迹里失败 0 条,而库里有 44 条。归因面板会因此
            # 长期空白,而空白读起来像"没有失败",恰好是最误导人的一种显示。
            failed = db.list_skill_traces(outcomes=["tool_error", "handoff"],
                                          limit=_TRACE_WINDOW)
            if failed:
                verdict = partition(failed, db.list_recent_archives(limit=200), db=db)
                for item in verdict["details"]:
                    bucket = failure_attribution.setdefault(item["skill_name"] or "", {})
                    bucket[item["category"]] = bucket.get(item["category"], 0) + 1
        except Exception:  # noqa: BLE001 归因算不出来就不显示,不拖累整份总览
            failure_attribution = {}

        return {
            "live": live,
            "candidates": candidates,
            "traces": traces,
            "traces_window": {
                "limit": _TRACE_WINDOW,
                "note": "按最近轨迹计数的窗口值,非全时段统计;窗口为所有 skill 共用,高频 skill 可能挤占低频 skill 的样本",
            },
            "failure_attribution": failure_attribution,
            "failure_attribution_note": (
                "失败轨迹按可修性分类:knowledge_gap=skill 缺知识(唯一会回流自改进的一类)/ "
                "capability_limit=权限或依赖边界(如归属校验拒绝跨用户访问、上游超时)/ "
                "evaluation_noise=不构成证据(会话过短、守卫拦截)/ undetermined=判不出。"
                "确定性规则判定,不调模型。"
                f"失败按 outcome 单独取最近 {_TRACE_WINDOW} 条,与上面的 traces 窗口互不挤占。"),
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

        from app.agent.skills import doc_distill as dd
        from app.agent.skills.risk import promotion_policy
        from app.agent.skills.tree_text import classify_tree_risk, validate_skill_tree
        from app.observability.langfuse_bridge import background_trace
        from app.observability.langfuse_client import make_openai_client
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
                # 阶段一 gap⑤:蒸馏这一次真调 LLM 的入口包进命名 trace,client 换成
                # drop-in 包装(门控关/未装时行为与裸 OpenAI 完全一致)。
                with background_trace("skills_distill", input={"doc_chars": len(doc)}):
                    client = make_openai_client(api_key=settings.openai_api_key,
                                                base_url=settings.openai_base_url)
                    out = dd.distill_from_doc(client, settings.model_name, doc, str(staging))
            except Exception as exc:  # noqa: BLE001 LLM/网络失败如实回传,不 500
                # 完整异常(可能带 base_url/代理等细节)只落服务端日志,回给客户端的只有类型名
                logger.exception("skills/distill 调用 LLM 失败")
                return {"created": False, "name": None, "risk": None, "policy": None,
                        "errors": [f"蒸馏失败，请稍后重试或联系管理员（{type(exc).__name__}）"],
                        "truncated": truncated}

            if out is None:
                # 只有"资料是空的"会走到这里(那种情况不调 LLM、不花钱)
                return {"created": False, "name": None, "risk": None, "policy": None,
                        "errors": ["资料内容为空,未生成任何候选"], "truncated": truncated}

            if not out.get("ok"):
                # **把精确原因交给操作者。** 改造前这里只回一句三选一的
                # 「frontmatter 不全 / 工具名不实 / 名字非法」——店主既不知道是哪一种,
                # 也不知道该改什么。实测真因往往只是资料里写了一个本店没有的工具名
                # (如 SOP 里的"走人工工单"被写成 `escalate_to_human`),而产物其余
                # 部分完全正确。
                unknown = list(out.get("unknown_tools") or [])
                errors = list(out.get("errors") or []) or ["LLM 产物未通过校验"]
                if unknown:
                    errors.append(
                        f"资料里提到的「{'、'.join(unknown)}」不是本店可用工具。"
                        "请把资料中对应的步骤改成用下方可用工具表述,或改写成不依赖"
                        "工具的话术指引(例如『告知买家将由人工跟进』)。")
                return {"created": False, "name": None, "risk": None, "policy": None,
                        "errors": errors, "unknown_tools": unknown,
                        "available_tools": list(out.get("available_tools") or []),
                        "attempts": out.get("attempts"), "truncated": truncated}

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
            # attempts==2 表示"第一次产物有问题、已自动带原因重试并修好了"。
            # 值得告诉操作者:他的资料里有个词和本店工具集对不上,下次写 SOP 可以避开。
            return {"created": True, "name": name, "risk": risk,
                    "policy": promotion_policy(risk), "errors": [],
                    "attempts": out.get("attempts"), "truncated": truncated}
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

        可观测性(阶段一 gap①):此端点此前直接调 `orch.chat()`,完全绕开
        `run_agent_streaming`,店主的每一轮对话在 Langfuse 里都是空白——
        分析师/增长两个画像回答了多少、调了什么工具、耗时多少,一概看不到。
        这里补一条与买家侧同等丰富度的 trace:开轮根 trace→挂 event_sink
        接住路由/工具调用等事件→结束后补发 reply/metadata(orch.chat() 本身
        不像 streaming.py 那样发这两类事件,需要在这里补齐)。全程 best-effort:
        `langfuse_turn` 门控关/未装/异常都返回 None,不影响店主本轮回复。
        """
        _require_seller_console()
        from contextlib import nullcontext
        from app.observability.langfuse_bridge import langfuse_turn
        sid = (req.session_id or "").strip() or "seller-default"
        orch = seller_sessions.get_or_create(sid, user_id="seller")
        lock = seller_sessions.get_lock(sid)
        lf_turn = langfuse_turn(sid, "seller", req.message or "")
        with lock:
            orch.event_sink = lf_turn.on_event if lf_turn is not None else None
            try:
                with (lf_turn if lf_turn is not None else nullcontext()):
                    result = orch.chat(req.message or "")
                    if lf_turn is not None:
                        lf_turn.on_event({"type": "reply", "content": result.reply})
                        lf_turn.on_event({"type": "metadata", "intent": result.intent.value,
                                          "confidence": result.confidence,
                                          "requires_human": result.requires_human})
            finally:
                orch.event_sink = None
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

    @app.post("/api/seller/stream", dependencies=[Depends(admin_auth)])
    async def seller_stream(req: SellerChatRequest):
        """店主与参谋的 SSE 流式对话。

        与买家侧 run_agent_streaming() 同一套事件协议(progress/stage/
        route/tool_call/reply/metadata/done),前端可复用同一 SSE 解析器。

        鉴权、会话管理与 /api/seller/chat 一致:admin_auth 门控、
        seller_sessions 独立 SessionManager、get_lock 防并发。
        """
        _require_seller_console()
        from app.api.streaming import run_seller_streaming
        sid = (req.session_id or "").strip() or "seller-default"
        orch = seller_sessions.get_or_create(sid, user_id="seller")
        lock = seller_sessions.get_lock(sid)
        return StreamingResponse(
            run_seller_streaming(orch, req.message or "", sid, lock),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- 店主通知(参谋异常诊断主动推送,前端轮询)----

    @app.get("/api/seller/notifications", dependencies=[Depends(admin_auth)])
    def seller_notifications(unread_only: bool = False, limit: int = 20):
        """获取店主通知列表。unread_only=True 时只返回未读。"""
        _require_seller_console()
        db = get_db()
        items = db.list_notifications(unread_only=unread_only, limit=limit)
        return {"notifications": items,
                "unread_count": db.count_unread_notifications()}

    @app.post("/api/seller/notifications/{nid}/read", dependencies=[Depends(admin_auth)])
    def mark_notification_read(nid: int):
        """标记单条通知已读。"""
        _require_seller_console()
        get_db().mark_notification_read(nid)
        return {"success": True}

    @app.post("/api/seller/notifications/read-all", dependencies=[Depends(admin_auth)])
    def mark_all_notifications_read():
        """一键全部已读。"""
        _require_seller_console()
        n = get_db().mark_all_notifications_read()
        return {"success": True, "marked": n}

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
        scan = anomaly_scan(window_days=window_days)
        return {
            "overview": shop_overview(window_days=window_days),
            "products": product_diagnostics(window_days=window_days, top_n=5),
            "anomalies": scan["anomalies"],
            # 扫描自带的口径与盲区一并下发。改造前这里只取 `["anomalies"]`,把
            # products_truncated / reviews_truncated / service_insufficient 全
            # 丢在 API 边界上——那几个键存在的唯一目的就是"让盲区可见",在这里
            # 被丢掉等于它们从没被写出来过。
            #
            # 两个窗口都要给:`quality` 是按经营窗(默认 7 天)算的服务质量,而
            # 告警按 service_window_days(默认 1 天)判。两个数会不一样,而且
            # **应该**不一样——控制台上必须看得出哪个是哪个,否则"7 天里坏过
            # 但今天已经好了"会被读成"看板自相矛盾"。
            "anomaly_scope": {
                "window_days": scan["window_days"],
                "service_window_days": scan["service_window_days"],
                "service_insufficient": scan["service_insufficient"],
                "products_examined": scan["products_examined"],
                "products_truncated": scan["products_truncated"],
                "reviews_examined": scan["reviews_examined"],
                "reviews_truncated": scan["reviews_truncated"],
            },
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
                # **永久性失败,不是抖动。** 这条通道是"把消息追加进买家自己的客服
                # 会话",买家从来没开过会话就没有可追加的地方,再点一次批准结果
                # 一样。此前它与"抢不到锁"、"落盘失败"塌成同一个 delivered=False,
                # 于是操作者收到的是一句"投递失败,已退回待审,可重试"——重试永远
                # 失败,而每一次都要花掉一次人工注意力。
                #
                # 实测撞到:走查时给 `walkthrough_buyer` 造了订单(直接写库,没聊过
                # 天),商机发现器照常挑出他 → 营销花一次 LLM 起草 → 人工审 → 批准
                # → 必然失败 → 提示可重试。整条链在一个**结构上不可达**的目标上
                # 空转。`_revert_after_failure` 本来就有 `retryable` 参数、它的
                # docstring 也写着"不能不管三七二十一都说可重试",只是投递这一路
                # 被硬编码成 True,这一种情形从那条纪律里漏了出去。
                return {"delivered": False, "warning": "",
                        "reason": "该买家没有客服会话,无法通过会话通道投递",
                        "retryable": False}
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

    def _delivery_result(raw) -> tuple[bool, str, str, bool]:
        """把投递函数的返回值归一成 (delivered, warning, reason, retryable)。

        `app.state.deliver_outreach` 是一个可替换的注入点(测试打桩、未来别的
        投递通道),历史签名返回裸 bool。裸 bool 只表达"送没送到",没有"送到了但
        记账有问题"这一档,按 warning 为空处理即可,语义无损且不会误判成失败。

        `reason` / `retryable` 是后加的:**失败原因不都是同一种性质**。"买家没有
        客服会话"是永久性的(重试永远失败),而"抢不到锁"、"落盘失败"是暂时的。
        塌成一个 bool 之后操作者一律被告知"可重试",于是在结构上不可达的目标上
        反复消耗人工注意力。缺省 `retryable=True` 保持既有行为:老式裸 bool 和
        没带这两个键的字典(包括测试里的桩)语义逐字节不变。
        """
        if isinstance(raw, dict):
            return (bool(raw.get("delivered")), str(raw.get("warning") or ""),
                    str(raw.get("reason") or ""),
                    bool(raw.get("retryable", True)))
        return bool(raw), "", "", True

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
        db = get_db()
        # 可达性缓存:同一个买家可能有多条待审草稿,一次列表不必为他重复查会话。
        reachable: dict[str, bool] = {}
        for r in rows:
            kind = r.get("opportunity_type") or ""
            r["opportunity_label"] = OPPORTUNITY_KINDS.get(kind, kind)
            code = (r.get("offer") or {}).get("coupon_code") or ""
            if code:
                info = COUPON_BY_CODE.get(code)
                r["coupon_discount"] = info["discount"] if info else ""
            # 可达性:与上面 coupon_discount **同一条原则**——把"批准之后会发生
            # 什么"在按钮按下**之前**摆出来,而不是让店主事后才知道。
            #
            # 投递通道是"把消息追加进买家自己的客服会话"(见 _deliver_outreach),
            # 买家从来没开过会话就没有可追加的地方,批准必定失败、且重试永远失败。
            # 而商机发现器读的是 orders/carts,与 conversations 无关——所以待审队列
            # 里本来就会混进结构上不可达的目标。实测走查时就撞到一条:营销花了一次
            # LLM 起草、人工审完批准,才发现送不出去。
            #
            # 这里**只标注不过滤**:买家没有客服会话不等于这条商机不成立(店铺可能
            # 有别的触达渠道,买家也可能明天就来咨询),悄悄丢掉是另一种错。
            uid = str(r.get("user_id") or "")
            if uid not in reachable:
                try:
                    reachable[uid] = bool(db.latest_conversation(uid))
                except Exception:  # noqa: BLE001 查不到就不标注,绝不因此让整张列表 500
                    logger.warning("查询买家会话失败,该条不标注可达性 user_id=%s",
                                   uid, exc_info=True)
                    reachable[uid] = True
            r["deliverable"] = reachable[uid]
            r["undeliverable_reason"] = (
                "" if reachable[uid] else "该买家没有客服会话,批准后无法投递(重试也不会成功)")
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

        (delivered, deliver_warning, deliver_reason,
         deliver_retryable) = _delivery_result(app.state.deliver_outreach(draft))
        if not delivered:
            # 原因跟着投递函数走,不在这里猜:同样是 delivered=False,"买家没有客服
            # 会话"重试永远失败,而抢锁/落盘失败重试是有意义的。文案与 retryable
            # 都必须如实,这正是 _revert_after_failure 那段 docstring 的纪律。
            return _revert_after_failure(
                f"投递失败:{deliver_reason}" if deliver_reason else "投递失败",
                retryable=deliver_retryable)

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

        # N7 接线:触达真的发生了 → 开一条跟进链(「持续沟通」的唯一起点)。
        #
        # 这一步之前是缺的,后果是整套跟进机制在生产里从不运行:`start_followup`
        # 全仓库只有测试在调,`outreach_followups` 表恒空 → `due_followups()`
        # 恒返回 [] → `run_due()` 恒返回全 0 → worker 的 `--followup` 是空转 →
        # 控制台「跟进」永远是空列表。代码、终止条件、端点、11 个测试全都在,
        # 只差一个启动点。
        #
        # 为什么接在这里而不是起草时:链的语义是"已经跟买家说过一次,过 N 小时
        # 没反应再提一次"。草稿可能被驳回、可能永远没人审——那种情况下买家根本
        # 没收到任何消息,"再提一次"无从谈起。只有投递成功(上面 `delivered`
        # 为真、消息不可撤销地送到了买家面前)才是这条链真正的第 0 步。
        #
        # 复用草稿自己的 correlation_id:`followup._last_touch_converted` 正是
        # 按 correlation_id 去 outreach_drafts 里找"这条链上一次触达判没判成
        # converted",两边必须是同一个 id 才对得上。
        #
        # fail-soft:起链失败绝不推翻"消息已经真实投递"这个不可撤销的事实——
        # 与上面写归因基线同一姿态,只记日志。返回 None 有两种情况,都不是错误:
        # 该买家该 kind 已有一条 active 链(唯一索引兜底,一人一类型一条链),
        # 或并发下被另一次审批抢先建了。
        try:
            _fu_kind = (draft.get("opportunity_type") or "").strip()
            _fu_user = (draft.get("user_id") or "").strip()
            if marked and _fu_kind and _fu_user:
                _fid = db.start_followup(
                    _fu_user, _fu_kind,
                    draft.get("correlation_id") or bus.new_correlation_id("FOLLOWUP"),
                    max_steps=settings.followup_max_steps,
                    interval_hours=settings.followup_interval_hours)
                if _fid is None:
                    logger.info("跟进链未新建(该买家该类型已有进行中的链) "
                                "draft_id=%s user=%s kind=%s",
                                draft_id, _fu_user, _fu_kind)
        except Exception:  # noqa: BLE001 起链失败不得推翻已发生的投递
            logger.exception("开启跟进链失败(不影响已投递的消息) draft_id=%s", draft_id)

        bus.publish(bus.EV_OUTREACH_SENT,
                    {"draft_id": draft_id, "user_id": draft.get("user_id")},
                    bus.AGENT_HUMAN,
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
        from app.multi_agent import bus
        from app.multi_agent import shared_context as sc

        events = bus.timeline(correlation_id=correlation_id or None, limit=limit)
        # 通过 shared_context 模块统一读取,自动适配后端(sqlite/redis)。
        # Redis 后端时 correlation_id 过滤在 Python 层完成(数据量小,可接受);
        # SQLite 后端时仍在 SQL 里过滤(原行为不变)。
        shared = sc.list_shared_context(limit=limit,
                                        correlation_id=correlation_id or None)
        return {"success": True, "events": events, "shared": shared}

    @app.get("/api/admin/collab/health", dependencies=[Depends(admin_auth)])
    def collab_health(limit: int = 20):
        """协作链的健康出口:失败事件 + worker 心跳。

        补的是一个真实的可见性缺口。`bus.consume()` **刻意不自动重试** failed
        事件(避免一条坏事件无限循环),注释写的是"留在表里供人工在时间线上看到
        并决定"——但时间线端点必须先知道 correlation_id 才查得到。也就是说在这
        个端点之前,一条失败的协作链没有任何人会发现:没有告警、没有面板、没有
        列出入口。"留给人工决定"事实上是"留给没人"。

        心跳补的是另一半:`reclaim_stale_events` 能救"worker 认领后崩在半路"的
        单条事件,救不了"worker 进程整个死了"——那时协作静默停摆,而买家链路
        一切正常,不会有任何症状暴露出来。

        `stale_seconds` 由服务端算好下发,不让前端自己拿本地时钟去减:两边时钟
        不一致时前端会算出负数或夸张的数值,而这个数字是运维判断"要不要去看一眼"
        的唯一依据。`healthy` 同理——阈值口径必须只有一处。
        """
        _require_seller_console()
        from app.multi_agent import bus, collab
        from app.scripts.agent_collab import WORKER_NAME

        db = get_db()
        hb = db.get_worker_heartbeat(WORKER_NAME)
        stale = None
        last_ok = (hb or {}).get("last_success_at")
        if last_ok:
            try:
                stale = max(0, int((datetime.now() - datetime.strptime(
                    last_ok, "%Y-%m-%d %H:%M:%S")).total_seconds()))
            except (ValueError, TypeError):
                stale = None      # 时间戳格式异常不该让整个健康接口 500
        # 判活阈值取 worker 轮询间隔的若干倍:一轮没跑完就报警会天天误报。
        threshold = max(120, settings.reaper_interval * 3)
        return {
            "success": True,
            "failed_count": bus.failed_count(),
            "failed": bus.failed(limit=limit),
            "worker": {
                "name": WORKER_NAME,
                "last_success_at": last_ok,
                "last_error_at": (hb or {}).get("last_error_at"),
                "last_error": (hb or {}).get("last_error"),
                "stale_seconds": stale,
                "threshold_seconds": threshold,
                # never_ran(从未跑过)与 stale(跑过但停了)是两种不同的状况:
                # 前者多半是"还没部署 worker",后者是"部署了但挂了"。都判不健康,
                # 但前端要能分开提示,所以下发的是 last_success_at 而不只是布尔。
                "healthy": bool(last_ok) and stale is not None and stale <= threshold,
            },
            # 预算是**本进程**的计数器。API 进程查这个端点看到的恒为 0——真正花
            # 钱的是 worker 进程。不下发就等于这道闸在界面上不存在(运维不知道
            # 有上限、更不知道今天是不是已经被熔断了);下发但不说明进程边界,
            # 运维会看着 spent=0 得出"worker 没花过钱"的错误结论。所以 scope 是
            # 响应的一部分,不是注释。
            "budget": {**collab.budget_status(), "scope": "本 API 进程计数",
                       "note": "实际消耗发生在协作 worker 进程,此处恒为 0"},
            # 降级统计:参谋归因的 LLM 调用失败时会降级为纯统计,而事件本身正常
            # 走完 → 状态是 done。实测一轮 20 条全部降级,worker 报告的却是
            # `{claimed:20, done:20, failed:0}`,链上一片绿色。**一次完全无效的
            # 运行和一次健康的运行在界面上长得一模一样**,而降级的诊断按路由规则
            # 不会唤醒营销——整条链就这么静默停住,没有任何地方说得出为什么。
            "degraded": _collab_degraded_stats(),
        }

    @app.get("/api/admin/collab/inbox", dependencies=[Depends(admin_auth)])
    def collab_inbox(limit: int = 50):
        """人工闸待办:投给 `human` 但还没人处理的事件。

        补的是与 failed 事件同构、但更隐蔽的一个缺口。路由表把
        `action.drafts_ready` / `result.outreach_converted` / `result.outreach_no_change`
        都投给 `human`,理由写得清清楚楚("草稿必须经人工审批才会发出"、
        "转化结果供人工在工作台查看")——但 **worker 只消费 analyst 与 growth**,
        human 的事件没有任何消费方;而在这个端点之前,也**没有任何界面列出它们**。

        实测积压 325 条,状态永远是 pending。路由表上那句"投给人工"于是变成了
        一句没有落点的声明:事件写进去,再没有出口。
        """
        _require_seller_console()
        from app.multi_agent import bus

        db = get_db()
        return {"success": True,
                "pending": db.count_pending_for_target(bus.AGENT_HUMAN),
                "events": db.list_pending_for_target(bus.AGENT_HUMAN, limit=limit)}

    @app.post("/api/admin/collab/inbox/{event_id}/ack", dependencies=[Depends(admin_auth)])
    def collab_inbox_ack(event_id: int):
        """人工确认一条待办(pending → done)。

        光"看得见"不够——运营看到一条待办之后在此之前**做不了任何事**,
        它会永远停在列表里,新的待办被埋在下面。与失败事件必须有"放回队列"
        这个扳机是同一个道理。

        幂等靠条件更新:连点两次、两个人同时点,只有第一次真的改到状态。
        """
        _require_seller_console()
        changed = get_db().acknowledge_event(event_id)
        return {"success": True, "changed": changed,
                "message": "已确认" if changed else "该事件已不在待办状态(可能已被他人确认)"}

    def _collab_degraded_stats(limit: int = 50) -> dict:
        """最近若干条诊断里有多少是降级的。

        **必须从 `shared_context` 数,不能从 `insight.diagnosis` 事件数。**

        降级的诊断按路由规则不唤醒营销(`routing._marketing_worthy` 判 degraded
        直接返回 False),而 `resolve()` 返回空目标时 `publish()` **压根不会插入
        任何事件行**——于是降级诊断在事件总线上不留一丝痕迹。实测库里
        `insight.diagnosis` 事件只有 2 条,而 `shared_context` 里有 6 条诊断、
        其中 3 条是降级的。按事件数统计会得出"降级 0 条"这个恰好相反的结论。

        这也顺带说明了为什么这个出口是必要的:协作链上只看得到
        `signal.anomaly` 变成 done,然后什么都没有——链就这么断了,而界面上
        没有任何东西说得出为什么。
        """
        from app.multi_agent import shared_context as sc

        rows = sc.list_shared_context(prefix="diagnosis:", limit=limit)
        total = len(rows)
        bad = 0
        for r in rows:
            v = r.get("value")
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    v = {}
            if isinstance(v, dict) and v.get("degraded"):
                bad += 1
        return {
            "window": limit, "diagnoses": total, "degraded": bad,
            "rate": round(bad / total, 3) if total else 0.0,
            # 全降级是一个**明确的故障态**,不该靠人去比较两个数字才发现
            "all_degraded": bool(total and bad == total),
            "note": ("参谋归因的 LLM 调用失败时会降级为纯统计,事件仍算处理成功;"
                     "降级的诊断不会唤醒营销,且不会产生任何总线事件,"
                     "协作链会在此静默断掉"),
        }

    # ---------- Skill 转正 / 驳回 / 回滚(打通自进化闭环的最后一环) ----------
    #
    # **补的是"候选只进不出"这个缺口。** 改造前 skills 只有三个端点(列表/上传/
    # 蒸馏):候选能从界面产生,却**只能登进服务器敲 `python -m
    # app.scripts.promote_skill` 才上得线**。实测界面上躺着 7 个候选(5 个校验
    # 通过),产品内没有任何办法处理它们——7 步自进化闭环因此断在最后一环。
    #
    # 三个端点都**复用 `promote_skill` 的既有函数**,不在这里重写简化版:
    # `promote()` 已经处理过 TOCTOU(先把候选整树快照,校验/判档/装机只认那一份
    # 字节),而"网页能并发替换候选目录"恰恰是这个端点会引入的并发源。

    # 处理完的候选一律**归档不删**(转正 → `_promoted/`,驳回 → `_rejected/`):
    # 被驳回的是下一轮改进的输入,已转正的是"线上这份正文当时长什么样"的证据。
    # 归档目录常量由 `promote_skill` 统一持有(`ps.PROMOTED_DIR`/`ps.REJECTED_DIR`),
    # 这里不另存一份——两处各写一个路径迟早分叉。

    @app.post("/api/admin/skills/{skill_name}/promote",
              dependencies=[Depends(admin_auth)])
    async def admin_promote_skill(skill_name: str, force: bool = False,
                                  run_gate: bool = False,
                                  allow_fact_loss: bool = False):
        """把候选转正上线。保留全部既有关卡,只是把扳机搬到界面上。

        **门禁是显式选项,不是默认动作。** `gate_candidate()` 会真跑两轮评测
        (候选 vs 现行,各自真调 LLM),一次一两分钟且花钱——挂在一个网页按钮上
        同步等,既不合适也容易被代理/网关掐断。所以:

        - `run_gate=false`(默认):**不跑门禁**,传 `gate_result=None` 给
          `promote()`。它的既有行为是 fail-closed 拒绝("缺少门禁结果,拒绝转正")
          ——于是这个按钮**不会静默绕过门禁**,而是明确告诉操作者门禁没跑过。
        - `run_gate=true`:真跑门禁再按结果决定。慢、花钱,但语义与 CLI 完全一致。
        - `force=true`:只放行**评测门禁**,不放行校验(与 CLI `--force` 同义)。
          操作者据此在"我知道没跑门禁"的前提下放行。

        实测 7 个候选里有 5 个**根本没有门禁用例**(`gate_case_ids` 返回空),
        那种情况下门禁本身就会 fail-closed 拒绝。把这件事说清比让按钮看起来
        "有时能用有时不能"重要。

        **"评不了"与"评了没过"在响应里是两个字段。** `gate_evaluable=False` 表示
        门禁不具备评估条件(没有用例 / 评测崩了),候选**未被否证**;`gate=` 判定
        为不放行才是候选不达标。混成一句"门禁未通过"会让操作者去改一个没有问题的
        候选——而正确的动作是补用例或人工放行。候选列表(`GET /api/admin/skills`)
        已经在**点这个按钮之前**就带上了 `gate_cases`/`gate_evaluable`,数用例不
        花钱,没道理让人用两次 400 把它试出来。

        高危档(`risk=high`)在这里**不拦**:它的含义是"必须由人来放行",而人在
        界面上点这个按钮正是那个放行动作(与 CLI 人工路径一致,见 `promote()` 的
        `block_on_high` 注释)。前端负责二次确认并显示风险来源。
        """
        from app.agent.skills.risk import promotion_policy
        from app.scripts import promote_skill as ps

        def _do() -> dict:
            gate = None
            gate_note = "未跑评测门禁(run_gate=false)"
            # None = 没跑过门禁所以无从谈起;True/False = 门禁评了 / 门禁评不了。
            # 前端据此把"候选没通过"和"门禁没法评"渲染成两回事(后者不该让操作者
            # 去改候选)。默认 None 而不是 False:run_gate=false 时确实不知道。
            gate_evaluable: bool | None = None
            if run_gate:
                try:
                    from app.agent.skills.gate import (default_eval_fn, gate_candidate,
                                                       gate_readiness)

                    # 先查就绪度再决定要不要花钱跑:没有用例时直接把**为什么评不了、
                    # 以及人该做什么**说出来,不要跑一趟评测再回来说一句 no_gate_cases。
                    ready = gate_readiness(skill_name, settings.eval_dataset_path)
                    case_ids = ready["case_ids"]
                    if not case_ids:
                        gate_evaluable = False
                        # note 自己已经把"为什么评不了"和"这不是候选质量问题"说全了,
                        # 这里只补上"那人该做什么"——重复第二遍反而让人不读。
                        gate_note = (
                            f"{ready['note']};"
                            f"要么给评测集补一条点名 {skill_name} 的用例,要么用 force 人工放行")
                    else:
                        gate = gate_candidate(
                            skill_name=skill_name,
                            candidate_path=str(Path(ps.CANDIDATES_DIR) / skill_name / "SKILL.md"),
                            definitions_dir=ps.DEFINITIONS_DIR,
                            dest_root=ps.DEFINITIONS_DIR,
                            eval_fn=default_eval_fn, case_ids=case_ids)
                        gate_evaluable = gate.get("evaluable") is not False
                        gate_note = f"门禁({gate.get('case_count', len(case_ids))} 条用例): " \
                                    f"{gate.get('reason', '')}"
                except Exception as exc:  # noqa: BLE001 门禁自身出错按未通过处理
                    logger.warning("转正门禁执行失败 skill=%s: %s", skill_name, exc)
                    # 门禁自己崩了同样是"评不了",不是候选不达标。
                    gate_evaluable = False
                    gate_note = f"门禁执行失败(评不了,非候选质量问题): {exc}"
            # `allow_fact_loss` 与 `force` **必须分开**。界面上的「转正上线」按钮
            # 永远带 force=true(它默认不跑门禁,后端对 gate=None fail-closed),
            # 事实一致性若也挂在 force 上,这道闸在人最常走的那条路上就从来不生效。
            # 分开之后语义也更准:放行门禁是"我知道没测过",放行事实丢失是"我知道
            # 我在删哪几条硬事实" —— 两个不同的知情同意。
            r = ps.promote(skill_name, ps.DEFINITIONS_DIR, ps.CANDIDATES_DIR,
                           ps.ARCHIVE_DIR, gate, bool(force), ps._now_stamp(),
                           allow_fact_loss=bool(allow_fact_loss))
            r["policy"] = promotion_policy(r.get("risk")) if r.get("risk") else None
            r["gate_note"] = gate_note
            r["gate_evaluable"] = gate_evaluable
            if r.get("promoted"):
                # 清待审队列。候选目录同时是界面上的"待审队列",已转正的候选留在
                # 里面会让运营分不清哪些还要处理,重复点一次只会把版本号又推一格。
                # 归档失败**不改变"已转正"这个结论**——技能已经装上线了,只如实
                # 报出来让人手动收拾,不能把一次成功的转正回报成失败。
                r["archive"] = ps.archive_candidate(
                    skill_name, ps.CANDIDATES_DIR, ps.PROMOTED_DIR, ps._now_stamp())
            return r

        r = await run_in_threadpool(_do)
        if not r.get("promoted"):
            # 400 而不是 500:关卡拦下不是服务器故障。原因原样带出去,并附上门禁
            # 说明——否则操作者只看到"缺少门禁结果"不知道下一步该干什么。
            raise HTTPException(400, f"{r.get('reason') or '转正失败'}（{r.get('gate_note')}）")
        return {"success": True, **r}

    @app.post("/api/admin/skills/{skill_name}/reject",
              dependencies=[Depends(admin_auth)])
    async def admin_reject_skill(skill_name: str):
        """驳回候选:整目录移进 `_rejected/<name>-<时间戳>/`。

        **不删除**。被驳回的候选是下一轮改进的输入(自进化会重新读失败轨迹再合成),
        也是"为什么当时没上"的唯一记录。带时间戳是因为同一个 skill 可能被驳回多次。

        正在灰度的候选不能驳回:灰度期候选正文正在为一部分真实会话服务,把目录移走
        会让那些会话当场读不到文件(与 `_skill_canary_block` 同一条理由)。
        """
        from app.agent.skills.validator import is_safe_skill_name
        from app.scripts import promote_skill as ps

        if not is_safe_skill_name(skill_name):
            raise HTTPException(400, f"非法 skill 名,拒绝操作: {skill_name!r}")
        blocked = _skill_canary_block(skill_name)
        if blocked:
            raise HTTPException(409, blocked)

        # 与转正共用 `archive_candidate`,只是归档到 _rejected 而不是 _promoted:
        # 两条路径都是"这个候选处理完了,从待审队列摘掉",没有理由各写一份移动逻辑。
        r = await run_in_threadpool(
            ps.archive_candidate, skill_name, ps.CANDIDATES_DIR,
            ps.REJECTED_DIR, ps._now_stamp())
        if not r.get("archived"):
            raise HTTPException(400, r.get("reason") or "驳回失败")
        return {"success": True, "rejected": True, **r}

    @app.post("/api/admin/skills/{skill_name}/rollback",
              dependencies=[Depends(admin_auth)])
    async def admin_rollback_skill(skill_name: str):
        """把线上技能回滚到最近一次备份(转正前自动留的那份)。

        这是转正的对偶动作:没有它,"一键转正"就是一个**没有退路**的按钮——
        而技能正文直接决定客服说什么,上线后发现不对必须能立刻退回去,
        不该要求运营去登服务器。
        """
        from app.scripts import promote_skill as ps

        r = await run_in_threadpool(
            ps.rollback, skill_name, ps.DEFINITIONS_DIR, ps.ARCHIVE_DIR)
        if not r.get("rolled_back"):
            raise HTTPException(400, r.get("reason") or "回滚失败")
        return {"success": True, **r}

    # ---------- 知识库文档管理(代替去 ApeRAG 自己的页面上传) ----------
    #
    # 检索侧不变,仍是 `aperag_search` 读同一个 collection。这里只是把"写"这一半
    # 搬进本项目的管理端,免得店主为了改一份政策文档去开另一个系统。
    #
    # 实测确认过(scripts/aperag_write_smoke.py,真服务):API 写入的文档与 ApeRAG
    # UI 上传的文档落在同一个 collection、走同一条索引流水线、被同一次检索并排
    # 召回——不存在"两套东西"。

    #: 允许的文档扩展名。**在后端卡,不能只靠前端**。这个页面能改客服的政策依据,
    #: 传错一份文档,全店客服的回答口径当场就变了。
    _KB_ALLOWED_EXT = frozenset({".md", ".markdown", ".txt", ".pdf", ".docx"})
    #: 知识库文档的体积上限,独立于技能包上限(政策文档通常远小于技能压缩包)。
    _KB_MAX_BYTES = 10_000_000

    def _kb_collection() -> str:
        """知识库 collection id。**这里用的就是政策库那一个**(与检索侧同源)。

        与 `aperag_writer` 的"绝不回落 settings.aperag_collection_id"不冲突:
        那条约束针对的是**经验/教训/洞察**这类新内容——它们必须落在别的
        collection,因为 ApeRAG 的检索请求没有元数据过滤,混进政策库会让买家问
        退货政策时召回一段"历史话术"。而本端点管理的就是政策库本身,所以显式
        取这个 id 是对的。
        """
        cid = (settings.aperag_collection_id or "").strip()
        if not cid:
            raise HTTPException(503, "未配置 APERAG_COLLECTION_ID,知识库管理不可用")
        if not settings.aperag_api_key:
            raise HTTPException(503, "未配置 APERAG_API_KEY,知识库管理不可用")
        return cid

    @app.get("/api/admin/kb/documents", dependencies=[Depends(admin_auth)])
    async def kb_list_documents():
        """列出知识库文档及其索引状态。

        索引状态直接下发 ApeRAG 的原值,不在这里翻译成自己的一套词:
        `PENDING / CREATING / ACTIVE / DELETING / FAILED`。**终态是 ACTIVE 而不是
        COMPLETE**——本项目此前在两处把它写成了 COMPLETE,导致轮询永远等不到终态
        (见 aperag_writer 模块注释)。多翻译一层就多一处会和上游枚举漂移的地方。
        """
        from app.knowledge import aperag_writer as w

        cid = _kb_collection()
        try:
            docs = await run_in_threadpool(w.list_documents, cid)
        except w.KnowledgeBaseTimeout:
            # **超时不等于连不上,提示必须指向不同的动作。** 实测:刚上传完一篇文档
            # 之后的一段窗口里这里连续 30 秒超时,而 ApeRAG 直连 0.5 秒就返回 200
            # ——它只是在忙着建索引。原来两者共用一句"请确认 ApeRAG 已启动",会把人
            # 指去重启一个健康的服务,而正确的动作是等几十秒再刷新。
            #
            # 用 503 而不是 502:这是"暂时不可用、稍后重试",不是"上游坏了"。
            raise HTTPException(
                503, "知识库服务响应超时（通常是刚上传的文档正在建索引），"
                     "请稍等几十秒后刷新；若持续如此再检查 ApeRAG 负载")
        if docs is None:
            # 读不到与"知识库是空的"是两件事,不能都返回空列表(全项目同一条口径)
            raise HTTPException(502, "知识库服务连不上，请确认 ApeRAG 已启动")
        return {"success": True, "collection_id": cid,
                "base_url": settings.aperag_base_url,
                "documents": [{
                    "id": d.get("id") or d.get("document_id"),
                    "name": d.get("name") or d.get("filename"),
                    "size": d.get("size"),
                    "status": d.get("status"),
                    "vector_index_status": d.get("vector_index_status"),
                    "fulltext_index_status": d.get("fulltext_index_status"),
                    "created": d.get("created") or d.get("created_at"),
                } for d in docs]}

    @app.post("/api/admin/kb/documents/upload", dependencies=[Depends(admin_auth)])
    async def kb_upload_document(file: UploadFile = File(...)):
        """上传一份文档进知识库,**上传即确认**(一次调用走完 upload + confirm)。

        为什么不把 upload 与 confirm 拆成两个端点交给前端串:实测未 confirm 的
        文档**不出现在 `list_documents` 里**。也就是说 upload 成功、confirm 失败
        (或前端中途关页面)会留下一份**查不到、也没法在界面上删掉的孤儿**,
        还占着临时区。ApeRAG 拆两步是为了"先批量传、人工确认后入库"这种场景,
        而这个页面是一次一份、传了就是要用,没有中间态可言。

        返回后**索引仍在异步建**(实测 PENDING → CREATING → ACTIVE 约 15 秒),
        所以这里不等它完成:等着就把一次上传变成 15 秒的同步阻塞。前端按
        `vector_index_status` 轮询即可。
        """
        name = (file.filename or "").strip()
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext not in _KB_ALLOWED_EXT:
            raise HTTPException(
                415, f"不支持的文件类型 {ext or '(无扩展名)'}；"
                     f"允许:{'、'.join(sorted(_KB_ALLOWED_EXT))}")

        # 分块读并随读随判:一次性 read() 会先把整个请求体读进内存,那样体积上限
        # 约束不到内存占用(与技能包上传同一手法)。
        buf = bytearray()
        while True:
            chunk = await file.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > _KB_MAX_BYTES:
                raise HTTPException(413, f"文件过大(上限 {_KB_MAX_BYTES // 1_000_000} MB)")
        if not buf:
            raise HTTPException(422, "文件是空的")

        cid = _kb_collection()

        def _do() -> dict:
            from app.knowledge import aperag_writer as w

            doc_id = w.upload_document_bytes(cid, name, bytes(buf))
            if not doc_id:
                return {"success": False, "reason": "上传失败，请查看服务端日志"}
            n = w.confirm_documents(cid, [doc_id])
            if n < 1:
                # 上传成功但确认失败:文档停在 UPLOADED,**永远不会被检索到**,
                # 而且不出现在列表里(实测)。如实报错并把 id 带出去,让人还能
                # 手动处理,而不是留一个查不到的孤儿。
                return {"success": False, "document_id": doc_id,
                        "reason": "已上传但确认入库失败，该文档不会被检索到"}
            return {"success": True, "document_id": doc_id, "name": name,
                    "note": "索引正在异步创建（约 15 秒），状态变为 ACTIVE 后才会被召回"}

        return await run_in_threadpool(_do)

    @app.delete("/api/admin/kb/documents/{doc_id}", dependencies=[Depends(admin_auth)])
    async def kb_delete_document(doc_id: str):
        """删除一份知识库文档。

        这是**不可逆**动作,而且影响面比它看起来大:删掉一份政策文档之后,客服
        对该类问题的回答就失去了依据(退化成模型常识)。二次确认放在前端,
        这里只保证如实报告成败。
        """
        from app.knowledge import aperag_writer as w

        cid = _kb_collection()
        ok = await run_in_threadpool(w.delete_document, cid, doc_id)
        if not ok:
            raise HTTPException(502, "删除失败（文档可能已不存在，或知识库服务异常）")
        return {"success": True, "document_id": doc_id}

    @app.get("/api/admin/collab/chains", dependencies=[Depends(admin_auth)])
    def collab_chains(limit: int = 30):
        """最近的协作链清单(时间线的入口)。

        补的是和 `collab_health` 同构的一个缺陷:`collab_timeline` 要求调用方
        先知道 correlation_id,但在此之前没有任何地方列出过它。一条协作链除非
        恰好失败(才会出现在 failed 列表里),否则没有任何入口能找到它——
        "多 Agent 到底协作了什么"的唯一出口,实际上只对失败的链开放。
        """
        _require_seller_console()
        return {"success": True, "chains": get_db().list_event_chains(limit=limit)}

    @app.get("/api/admin/collab/routing", dependencies=[Depends(admin_auth)])
    def collab_routing():
        """路由订阅表 + 消费闸 + Agent 名册。

        `routing.describe()` 的注释写着"供文档/管理端展示",但在此之前没有任何
        调用方——这份表是"多 Agent 到底怎么协作"的权威声明,却只有翻代码才读得到。

        一并下发 Agent 名册:订阅表里出现的是 `analyst`/`growth` 这类内部标识,
        管理端要渲染中文就得有一份映射,而那份映射**不能由前端另抄**——多一份
        副本就多一处会漂移的口径(与 OPPORTUNITY_KINDS 同理)。
        """
        _require_seller_console()
        from app.multi_agent import bus, routing

        return {
            "success": True,
            "subscriptions": routing.describe(),
            "gates": routing.describe_gates(),
            "agents": [
                {"key": bus.AGENT_SERVICE, "label": "客服 Agent",
                 "side": "buyer", "desc": "买家会话侧,唯一直接对买家说话的角色"},
                {"key": bus.AGENT_ANALYST, "label": "参谋 Agent",
                 "side": "seller", "desc": "只读经营归因,不落任何写操作"},
                {"key": bus.AGENT_GROWTH, "label": "营销 Agent",
                 "side": "seller", "desc": "只产草稿,发送权不在它手上"},
                {"key": bus.AGENT_HUMAN, "label": "人工闸",
                 "side": "human", "desc": "不可逆动作的唯一出口"},
            ],
        }

    @app.post("/api/admin/collab/failed/{event_id}/retry",
              dependencies=[Depends(admin_auth)])
    def collab_retry_failed(event_id: int):
        """把一条失败事件放回待处理队列,由下一轮 worker 重新认领。

        为什么要有这个动作:光"看得见"不够——运营看到一条失败的协作链之后,
        在此之前**做不了任何事**,只能干看着。系统刻意不自动重试(坏事件会无限
        循环),那就必须给人一个手动扳机,否则"留给人工决定"里的"决定"是空的。

        幂等靠数据层条件更新(failed → pending 只对仍是 failed 的行生效):
        连点两次、两个运营同时点,只有第一次真的改到状态,第二次拿到
        changed=False 而不是把一条已经在跑的事件again 打回队列。
        """
        _require_seller_console()
        from app.multi_agent import bus

        changed = bus.retry(event_id)
        return {"success": True, "changed": changed,
                "message": ("已放回队列,下一轮 worker 会重新处理"
                            if changed else "该事件不在失败状态(可能已被处理或已重试)")}

    @app.get("/api/admin/customer/{user_id}/orders", dependencies=[Depends(admin_auth)])
    def admin_customer_orders(user_id: str):
        """坐席侧:某个客户的订单清单。

        补的是坐席台上最刺眼的一个空白——**接待人看不到客户买了什么**。
        改造前右侧客户面板只有会话 ID / 轮次 / 最后活跃这类会话元数据,而买家
        开口第一句几乎总是关于某一笔订单。坐席要么去问买家"您的订单号是多少",
        要么切到别的系统查——两者都是把 AI 已经知道的事情重新问一遍人。

        **与买家自己的 `/api/orders` 共用同一条读取路径**(`_hmdp_my_orders` /
        `list_user_orders`),不另写一份查询:两边看到的必须是同一份事实,否则
        坐席据以答复的内容和买家屏幕上显示的对不上,比看不到更糟。

        鉴权走 admin_auth:这是**跨用户**读取,买家 token 拿不到别人的订单。
        """
        orders: list[dict] = []
        degraded = ""
        token = _hmdp_token_for_user(user_id)
        if token:
            try:
                orders = _hmdp_my_orders(token)
            except Exception as exc:  # noqa: BLE001 坐席面板不该因订单读不到而整块 500
                logger.warning("坐席读客户订单失败 user=%s: %s", user_id, exc)
                degraded = (f"订单服务不可用({type(exc).__name__})，"
                            f"下方来自本地订单库，可能不是最新")
        # hmdp 拿不到就回落本地订单库——与 `/api/orders` 同一条回退路径。
        # **失败时也必须回落**:坐席宁可看到一份可能过时的订单(并被明确告知),
        # 也好过面对一片空白去问买家"您的订单号是多少"。
        if not orders:
            try:
                raw = [o for o in get_db().list_orders() if o.get("user") == user_id]
                raw.sort(key=lambda o: o.get("created_at") or "", reverse=True)
                orders = [_fmt_order(o) for o in raw]
            except Exception as exc:  # noqa: BLE001
                logger.warning("坐席读客户订单失败(本地库) user=%s: %s", user_id, exc)
                degraded = f"订单库读取失败({type(exc).__name__})"
        # 空列表有两种含义(这个客户确实没下过单 / 订单服务连不上),必须分开。
        return {"success": not degraded, "user_id": user_id,
                "orders": orders, "degraded": degraded}

    @app.get("/api/handoffs", dependencies=[Depends(admin_auth)])
    def handoffs():
        """原始升级记录(逐条)。**这是审计视图,不是坐席工作队列。**

        坐席该用的是下面的 `/api/handoffs/sessions`——实测这里 50 条只来自 7 个会话,
        直接拿它当队列渲染会让队列深度失真 7 倍。保留本端点不变:每次升级确实发生过,
        逐条记录是审计与复盘的依据,不该为了界面好看就把它改掉。
        """
        return hitl.queue.list_pending() if hitl else []

    @app.get("/api/handoffs/sessions", dependencies=[Depends(admin_auth)])
    def handoff_sessions():
        """坐席工作队列:**一个买家在等 = 一行**,按等待时长升序(等最久的排最前)。

        见 `HandoffQueue.list_pending_sessions` 的完整说明。每行带 `escalations`
        (这个会话累计升级了多少次——反复升级本身就是优先级信号)与 `waiting_since`
        (最早那次升级,排队看这个而不是最近一次)。
        """
        if hitl is None:
            return {"sessions": [], "waiting_buyers": 0, "escalations_total": 0}
        sessions = hitl.queue.list_pending_sessions()
        return {
            "sessions": sessions,
            # 两个数都给:一个是"多少人在等"(坐席排班看它),一个是"累计升级次数"
            # (质量分析看它)。混成一个数正是这次要修的那个错。
            "waiting_buyers": len(sessions),
            "escalations_total": sum(s["escalations"] for s in sessions),
        }

    @app.post("/api/handoffs/{handoff_id}/resolve", dependencies=[Depends(admin_auth)])
    def resolve_handoff(handoff_id: str):
        ok = hitl.queue.resolve(handoff_id) if hitl else False
        return {"status": "resolved" if ok else "not_found"}

    @app.post("/api/handoffs/session/{session_id}/resolve",
              dependencies=[Depends(admin_auth)])
    def resolve_handoff_session(session_id: str):
        """把一个会话的全部待处理升级一次标记为已解决。

        坐席处理的是"这个买家",不是"这一次升级判定"。逐条 resolve 会让一个买家需要
        点 37 次(实测),而中间任何一次遗漏都会让这个会话重新出现在队列里。
        """
        n = hitl.queue.resolve_session(session_id) if hitl else 0
        return {"status": "resolved" if n else "not_found", "resolved": n}

    @app.post("/api/session/{session_id}/takeover", dependencies=[Depends(admin_auth)])
    def takeover(session_id: str):
        if hitl is None:
            return {"mode": "auto"}
        return {"mode": hitl.manual_mode.toggle(session_id)}

    @app.post("/api/reflow", dependencies=[Depends(admin_auth)])
    def reflow(limit: int = 200):
        # 带上 stats:回流会丢掉"去掉不可复现期望后一条断言都不剩"的候选,
        # 丢了多少必须让操作者看见,否则页面上那句"共回流 N 条"读起来像
        # "线上就这么点问题"(见 collect_reflow_cases 的说明)。
        if not store:
            return {"count": 0, "cases": [], "stats": None}
        cases, stats = collect_reflow_cases(store, limit=limit, with_stats=True)
        return {"count": len(cases), "cases": cases, "stats": stats}

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
