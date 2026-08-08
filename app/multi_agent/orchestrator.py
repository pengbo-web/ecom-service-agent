"""Multi-Agent 编排器:Router → 领域画像 → 同一个硬化引擎(EcomAgent)。

生产最主流的"薄编排"形态:不是 N 个各自带一套 ReAct 循环的子 Agent,而是
**同一个经过 Phase 1–6 硬化的引擎**,按路由结果切换"画像"(专属 system prompt + 工具子集)。
好处:复用全部硬化(空回复/畸形降级、落盘指针、consent 门、事件流、记忆/持久化),
延迟/成本低,加新领域只是加一份画像。对外接口与 EcomAgent 一致。
"""

import logging
from pathlib import Path
from typing import Optional

from app.config.settings import settings
from app.multi_agent.agents import AGENT_CONFIGS
from app.multi_agent.router import DEFAULT_AGENT, Router
from app.agent.tools.manager import ToolManager

# L3①:KB 并发预取专用的进程级共享线程池——惰性单例,daemon 线程不阻塞进程退出。
# 只提交一类任务(kb_fetch_rows,纯读:只读 settings + 买家原句,发一次独立的
# 检索 HTTP 请求,不 touch 任何每轮可变状态),max_workers 给够并发会话量级,
# 避免高并发下互相排队反而比不并发还慢。
_KB_PREFETCH_EXECUTOR = None


def _kb_prefetch_executor():
    global _KB_PREFETCH_EXECUTOR
    if _KB_PREFETCH_EXECUTOR is None:
        import concurrent.futures
        _KB_PREFETCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=32, thread_name_prefix="kb-prefetch")
    return _KB_PREFETCH_EXECUTOR


class MultiAgentOrchestrator:
    """**总控 Agent(Controller Agent)**:系统对外的唯一 Agent 入口。

    它把用户请求路由到领域画像(售前/售中/售后),用同一个经过硬化的 ReAct 引擎
    (EcomAgent)执行,并统一内聚以下四项职责,对外只读暴露(不改变底层行为):

    - **react**   :底层 ReAct 引擎(EcomAgent,含 `_react_loop` / 工具循环)。
    - **memory**  :记忆管理(短期/长期记忆巩固与召回)。
    - **permissions**:权限与安全门(风险动作 consent、幂等、升级)。
    - **lifecycle**:生命周期(save / close / reset)与当前运行状态。

    委托历史/记忆/持久化给引擎;对外接口与 EcomAgent 一致。
    调用 `capabilities()` 可列出这四项职责名称。
    """

    def __init__(self, session_path: Optional[str] = None, user_id: Optional[str] = None):
        from app.agent.chat import EcomAgent
        sid = Path(session_path).stem if session_path else None
        self.engine = EcomAgent(session_path=session_path, session_id=sid, user_id=user_id)
        self.router = Router(self.engine.client, self.engine.model)
        self._last_key: str | None = None   # 粘性路由:QU 未判定 domain 时沿用上轮

        # 每个画像 = 专属 prompt + 工具子集(独立 ToolManager,仅暴露该领域允许的工具)。
        # base_prompt(不含风格头/安全规则的领域正文)供 chat() 每轮按店主当前
        # 语气重新拼接；prompt(默认语气版)留作 fail-soft 回落目标。
        self.profiles: dict[str, dict] = {}
        for key, cfg in AGENT_CONFIGS.items():
            self.profiles[key] = {
                "name": cfg["name"],
                "prompt": cfg["prompt"],
                "base_prompt": cfg["base_prompt"],
                "tool_manager": ToolManager(
                    use_mcp=settings.mcp_enabled,
                    mcp_server_url=settings.mcp_server_url,
                    allowed_tools=cfg["tools"],
                ),
            }
        self._default_tm = self.engine.tool_manager   # 引擎自带的全量工具(复位/关闭用)

        # 供 streaming 层设置/透传(与 EcomAgent 接口一致)
        self.event_sink = None
        self.client = self.engine.client
        self._turn_stream_eligible = False   # E1:见 set_turn_stream_eligible

    def set_turn_stream_eligible(self, eligible: bool) -> None:
        """E1:streaming.py 每轮在调 chat() 前对着总控注入(它才是
        run_agent_streaming 拿到的 agent);chat() 里再原样转给真正跑
        ReAct 循环的引擎(self.engine)。"""
        self._turn_stream_eligible = bool(eligible)

    def chat(self, user_input: str):
        # 统一查询理解(默认):一次调用出 domain/intent/need_kb/kb_query,
        # 替代独立路由;关开关=回退老 Router(每轮必检索,无门控无改写)
        if settings.query_understanding_enabled:
            from app.agent.progress import emit_progress
            from app.agent import understanding
            emit_progress(self.event_sink, "understanding")   # L3③:如实上报——这一步真的要调 LLM 了
            kb_future = None
            if settings.qu_recall_concurrent_enabled:
                # L3①(关键):KB 预取只**提交**、不在这里等它跑完——真正的并发
                # 收益来自"understand() 这一步同步跑的时候,KB 检索已经在另一
                # 个线程里独立推进",而不是"两边都跑完再往下走"(那样等于比
                # 谁慢就等谁,反而可能比串行还慢——已用真实 ApeRAG 实测验证过
                # 这个反例,教训记在报告里)。返回的 Future 交给引擎,真正消费
                # 它(阻塞等待)的时机推迟到 `_build_messages` 真正需要检索结果
                # 的那一刻——这中间(FAQ 缓存查询/技能预加载等)本身也要花时间,
                # 进一步稀释了需要等待的那一段。
                kb_future = self._submit_kb_prefetch(user_input)
                if kb_future is not None:
                    emit_progress(self.event_sink, "retrieving")   # 确实提交了才说"正在检索"
            # 用 self.client(streaming 层每轮注入的 TracingClient):QU 调用进当前 trace,
            # token/延迟完整入账;engine.client 在下面才被覆盖,用它会漏记首轮。
            # 这次调用本身就在"当前"线程同步执行——KB 预取(如果提交了)已经在
            # 另一个线程独立跑着,不需要为了"并发"额外把这次调用也挪到线程里。
            qu = understanding.understand(user_input, self.engine.raw_messages,
                                          self.client, self.engine.model)
            key = qu.domain or self._last_key or DEFAULT_AGENT
        else:
            qu = None
            kb_future = None
            key = self.router.route(user_input, self.engine.raw_messages)
        self._last_key = key
        if qu is not None and qu.domain is None:
            qu.domain = key          # 粘性解析结果回填:检索过滤拿到确定域
        self.engine.set_turn_understanding(qu)
        # L3①:预取的 Future 是否可复用,交给 engine._build_messages 在**真正
        # 需要检索结果的那一刻**按"最终检索 query 是否与预取时完全相同"精确
        # 判定(见 EcomAgent.set_turn_kb_prefetch_future 的文档)——这里只负责
        # 搬运,不阻塞。没有提交预取(关开关/QU 关闭/原句过短)时必须显式 clear,
        # 不能让上一轮的 Future 残留在引擎上被误用(跨轮泄漏,见
        # EcomAgent.clear_turn_kb_prefetch 的文档)。
        if kb_future is not None:
            self.engine.set_turn_kb_prefetch_future(user_input, kb_future)
        else:
            self.engine.clear_turn_kb_prefetch()
        profile = self.profiles.get(key) or next(iter(self.profiles.values()))
        # E1:streaming.py 对着「总控」调 set_turn_stream_eligible(它才是
        # run_agent_streaming 传入的 agent),这里原样转给真正跑 ReAct 循环
        # 的引擎——否则生产路径(app.py 走的就是这个总控)永远读不到这个开关,
        # 只有裸 EcomAgent 测试才会生效。
        self.engine.set_turn_stream_eligible(getattr(self, "_turn_stream_eligible", False))
        if self.event_sink:
            event = {"type": "route", "agent": profile["name"], "key": key}
            if qu is not None:
                event.update(intent=qu.intent, need_kb=qu.need_kb, source=qu.source)
            self.event_sink(event)
        # 切画像:同一硬化引擎,换 prompt + 工具子集;透传 event_sink/client(含 tracer 包装)
        self.engine.system_prompt = self._build_system_prompt(profile)
        self.engine.tool_manager = profile["tool_manager"]
        self.engine.event_sink = self.event_sink
        self.engine.client = self.client
        return self.engine.chat(user_input)

    def _submit_kb_prefetch(self, user_input: str):
        """L3①:把 KB 检索(用买家原句)**提交**到共享线程池,立即返回 Future,
        不阻塞——真正的并发收益就在这个"不阻塞"上:调用方(chat())紧接着
        同步跑 understand(),这次 LLM 调用与 KB 检索天然重叠在同一段挂钟时间
        里,谁都不用等谁。

        实测教训(第一版实现踩过的坑,记在这里免得回头复犯):第一版在这个
        方法内部把 understand() 也扔进另一个线程,然后 `t1.join(); t2.join()`
        两个都等完才返回——用真实 ApeRAG(2~4s,偶发到 10s+)实测后发现,当
        QU 命中规则快筛(近 0 成本)时,总耗时被"等 KB 检索这个更慢的线程"
        完全主导,比老的串行路径(先 QU 后检索,检索本身耗时不变但至少不用
        多等一次线程调度)还慢——"并发"被做成了"谁慢等谁",完全违背初衷。
        现在的写法把"等结果"这件事推迟到 `_build_messages` 真正需要检索
        结果的那一刻(那时候 FAQ 缓存查询/技能预加载等也已经花掉了一些挂钟
        时间,进一步缩短真正需要阻塞等待的窗口)。

        return None 的两种情况,与 kb_fetch_rows 的早退分支同口径:检索总
        开关关闭、或原句短于 recall_kb_min_query_chars——这两种情况下"提交
        一个必然什么都不做的任务"没有意义,直接不提交,调用方不会因此发出
        "正在检索"这类不真实的进度提示。
        """
        from app.agent.recall.kb import kb_fetch_rows

        if not settings.recall_kb_enabled:
            return None
        if len((user_input or "").strip()) < settings.recall_kb_min_query_chars:
            return None

        def _run():
            try:
                return kb_fetch_rows(user_input)
            except Exception:
                # 预取失败=没有可复用的行,回退现场检索——现场检索(build_recall_
                # sections 内部)自有一套同样的容错,不在这里重复处理/上报。
                return None

        return _kb_prefetch_executor().submit(_run)

    def _build_system_prompt(self, profile: dict) -> str:
        """每轮按店主当前语气组装 system prompt(N1:品牌语气可配置)。

        这是承重改动点:原来 PRESALE_PROMPT 等常量在**模块导入时**就拼好了风格头,
        店主改语气不会生效。这里改成买家每轮 chat() 时才组装:读店铺人格 →
        渲染风格块 → build_profile_prompt 拼上安全规则与领域正文——顺序即安全
        边界(风格块在前,安全规则在后,店主文本压不过"不代客下单/不许编造")。

        fail-soft:读配置/渲染/拼接任何一步出错,都直接回落 profile["prompt"]
        (导入时就拼好的默认语气版)——这是买家对话的热路径,配置表读不出来
        绝不能让这一轮对话失败。
        """
        try:
            from app.config.shop_profile import load_profile, render_style_block
            from app.prompts.agents import build_profile_prompt
            style_block = render_style_block(load_profile())
            return build_profile_prompt(profile["base_prompt"], style_block)
        except Exception:  # noqa: BLE001 配置读取/拼接失败不能让买家会话失败
            logging.getLogger(__name__).warning(
                "店铺语气组装失败,回落默认语气 prompt", exc_info=True)
            return profile["prompt"]

    # ---- 委托给引擎(对外接口与 EcomAgent 一致)----
    @property
    def raw_messages(self) -> list:
        return self.engine.raw_messages

    @property
    def _pending(self):                      # R3:挂起动作透传给引擎(streaming 读/清)
        return self.engine._pending

    @_pending.setter
    def _pending(self, value):
        self.engine._pending = value

    @property
    def _turn_qu(self):                      # 查询理解结果透传(streaming 升级判定读)
        return self.engine._turn_qu

    @property
    def memory_manager(self):
        return self.engine.memory_manager

    @property
    def skill_manager(self):                 # 委托:CLI /skills 等按引擎的技能管理器工作
        return self.engine.skill_manager

    @property
    def session_id(self):
        return self.engine.session_id

    @property
    def user_id(self):
        return self.engine.user_id

    @property
    def history_size(self) -> int:
        return self.engine.history_size

    # ---- 总控 Agent 的四项职责入口(只读暴露,不改行为)----
    @property
    def react(self):
        """ReAct 引擎(EcomAgent,含 _react_loop / 工具循环)。"""
        return self.engine

    @property
    def memory(self):
        """记忆管理(短期/长期记忆)。"""
        return self.engine.memory_manager

    @property
    def permissions(self) -> dict:
        """权限与安全门:风险动作 consent、幂等、升级。"""
        from app.agent.consent import RISK_ACTIONS
        return {
            "risk_actions": RISK_ACTIONS,
            "consent": True,
            "idempotency": True,
            "escalation": True,
        }

    @property
    def lifecycle(self) -> dict:
        """生命周期视图:save/close/reset(可调用)+ 当前运行状态(status/step_seq)。"""
        return {
            "save": self.save,
            "close": self.close,
            "reset": self.reset,
            "status": self.engine._status,
            "step_seq": self.engine._step_seq,
        }

    def capabilities(self) -> list:
        """列出总控 Agent 内聚的四项职责名称。"""
        return ["react", "memory", "permissions", "lifecycle"]

    def reset(self):
        self.engine.reset()

    def save(self):
        self.engine.save()

    def close(self):
        self.engine.tool_manager = self._default_tm   # 复位后由 engine.close 关闭
        self.engine.close()
        # 逐个关闭每个画像的 tool_manager:单个失败要 catch 住继续关下一个,
        # 否则一个画像的 close() 抛异常就会让后面的画像永久漏关(资源泄漏)。
        # 与 SellerOrchestrator.close() 同一写法——那边先修好并留了一句"买家侧
        # 还有同款隐患"的注释,而一个有人写明的已知泄漏比直接修掉它更糟。
        for p in self.profiles.values():
            try:
                p["tool_manager"].close()
            except Exception:  # noqa: BLE001 单个画像关不掉不能连累其余
                pass


class SellerOrchestrator:
    """**卖家侧总控**:与 MultiAgentOrchestrator 同构,但服务对象是店主。

    actor 分流是确定性的——由端点决定走哪个编排器,不让 LLM 猜"这是买家还是店主"。
    只有 actor 内部的域路由(analyst/growth)才用 LLM。

    刻意复用同一个硬化引擎 EcomAgent:空回复降级、落盘指针、事件流、持久化
    全部照旧;卖家侧的差异只在 prompt + 工具子集 + 注入的共享上下文。

    "注入的共享上下文"指:每轮把 shared_context 里最近的诊断结论经
    `render_context_block`(带数据围栏)拼到画像 prompt 后面 —— 参谋异步写下的
    归因,店主下一次开口时两个画像都能读到。这也是那道围栏在生产里唯一被真正
    走到的地方;它只是第一层,真正的兜底仍是"参谋工具全只读、营销产物必过人工"。
    """

    #: 每轮注入多少条共享上下文。取小值:这是每轮都要进 prompt 的固定开销,
    #: 而店主真正关心的永远是最近那几条诊断。
    SHARED_CONTEXT_LIMIT = 5

    def __init__(self, session_path: Optional[str] = None, user_id: Optional[str] = None):
        from app.agent.chat import EcomAgent
        from app.multi_agent.agents import SELLER_AGENT_CONFIGS
        from app.multi_agent.seller_router import SELLER_DEFAULT, SellerRouter

        sid = Path(session_path).stem if session_path else None
        self.engine = EcomAgent(session_path=session_path, session_id=sid, user_id=user_id)
        self.router = SellerRouter(self.engine.client, self.engine.model)
        self.last_agent_key: str = SELLER_DEFAULT   # 供外部只读查询"上一轮落到哪个画像"

        self.profiles: dict[str, dict] = {}
        for key, cfg in SELLER_AGENT_CONFIGS.items():
            self.profiles[key] = {
                "name": cfg["name"],
                "prompt": cfg["prompt"],
                "tool_manager": ToolManager(
                    use_mcp=settings.mcp_enabled,
                    mcp_server_url=settings.mcp_server_url,
                    allowed_tools=cfg["tools"],
                ),
            }
        self._default_tm = self.engine.tool_manager
        self.event_sink = None
        self.client = self.engine.client

    def chat(self, user_input: str):
        # SellerRouter.route() 的返回值域是 SELLER_AGENTS ∪ {SELLER_DEFAULT},
        # 永远是真值,不会是 None/""——不像买家侧 QU 那样可能判不出 domain,
        # 因此这里不需要(也不该有)"粘性路由回退上一轮"的 or 链。
        key = self.router.route(user_input, self.engine.raw_messages)
        self.last_agent_key = key
        profile = self.profiles.get(key) or next(iter(self.profiles.values()))
        if self.event_sink:
            self.event_sink({"type": "route", "agent": profile["name"], "key": key,
                             "actor": "seller"})
        self.engine.system_prompt = profile["prompt"] + self._shared_context_block()
        self.engine.tool_manager = profile["tool_manager"]
        self.engine.event_sink = self.event_sink
        self.engine.client = self.client
        return self.engine.chat(user_input)

    def _shared_context_block(self) -> str:
        """本轮要拼到画像 prompt 后面的共享上下文片段(没有就返回空串)。

        内容里可能含用户可控文本(买家咨询原文会进诊断摘要),所以一律走
        `render_context_block` 的数据围栏,与 app/agent/product_context.py 同手法。
        整段 fail-soft:读库失败/渲染失败都退化成空串,店主那一轮照常回答——
        共享上下文是锦上添花的参考,不是回答的前提。
        """
        from app.multi_agent import shared_context as sc

        try:
            entries = sc.recent_entries(sc.KEY_DIAGNOSIS,
                                        limit=self.SHARED_CONTEXT_LIMIT)
            return sc.render_context_block(entries)
        except Exception:  # noqa: BLE001 注入失败不该让卖家会话失败
            logging.getLogger(__name__).warning("共享上下文注入失败(本轮跳过)",
                                                exc_info=True)
            return ""

    @property
    def raw_messages(self) -> list:
        return self.engine.raw_messages

    @property
    def session_id(self):
        return self.engine.session_id

    @property
    def user_id(self):
        return self.engine.user_id

    @property
    def _pending(self):
        return self.engine._pending

    @_pending.setter
    def _pending(self, value):
        self.engine._pending = value

    def reset(self):
        self.engine.reset()

    def save(self):
        self.engine.save()

    def close(self):
        self.engine.tool_manager = self._default_tm
        self.engine.close()
        # 逐个关闭每个画像的 tool_manager:单个失败要 catch 住继续关下一个,
        # 否则一个画像的 close() 抛异常就会让后面的画像永久漏关(资源泄漏)。
        # (MultiAgentOrchestrator.close() 目前是同一个隐患,未在本次改动范围内。)
        for p in self.profiles.values():
            try:
                p["tool_manager"].close()
            except Exception:
                pass
