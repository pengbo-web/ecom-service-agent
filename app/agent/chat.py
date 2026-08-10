import json
from typing import Callable, Optional

from openai import OpenAI

from app.session.store import get_session_store
from app.agent.summarizer import summarize
from app.config.settings import settings
from app.prompts.customer_service import SYSTEM_PROMPT
from app.schemas.response import CustomerServiceResponse, IntentType
from app.agent.tools.manager import ToolManager
from app.agent.history_utils import estimate_tokens, sanitize_tool_pairs
from app.agent.tools.bargain import set_current_session
from app.agent.reply_pipeline import ReplyPipeline


# E1(回复流式化):OpenAI 流式 delta 里 tool_calls 是分片到达的(每片只带
# 一小段 name/arguments 字符串,按 index 累加),下面三个是最小的"仿造"容器，
# 拼完之后跟非流式 `response.choices[0].message` 形状一致——下游
# `_parse_tool_calls`/`_react_loop` 读的是 `.content` / `.tool_calls[i].id`
# / `.function.name` / `.function.arguments`，零改动即可复用。
class _StreamedFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _StreamedToolCall:
    def __init__(self, id_: str, name: str, arguments: str):
        self.id = id_
        self.function = _StreamedFunction(name, arguments)


class _StreamedMessage:
    def __init__(self, content: str, tool_calls: Optional[list]):
        self.content = content
        self.tool_calls = tool_calls or None


class EcomAgent:
    """内部 ReAct 引擎,由总控 Agent(MultiAgentOrchestrator)驱动;不再作为独立运行模式。

    承载工具循环 / 记忆 / consent / 持久化 / 事件流等全部硬化能力。总控 Agent 按路由
    切换画像(system prompt + 工具子集)后复用本引擎执行——它不再直接对外作为运行入口。
    """

    def __init__(self, session_path: Optional[str] = None, session_id: Optional[str] = None,
                 user_id: Optional[str] = None):
        # 模型容错:启用时用主备熔断代理(memory/summarizer 复用本 client 自动继承)
        if settings.resilience_enabled:
            from app.resilience.factory import make_resilient_client
            self.client = make_resilient_client()
        else:
            from app.observability.langfuse_client import make_openai_client
            self.client = make_openai_client(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
            )
        self.model = settings.model_name
        self.temperature = settings.temperature
        self.session_path = session_path or settings.session_path
        self.session_id = session_id
        self.user_id = user_id or settings.memory_user_id   # 长期记忆按用户隔离
        self.system_prompt = SYSTEM_PROMPT   # 可切换:多 Agent 编排按路由画像覆盖
        self._turn_recall = None   # (last_user, RecallResult) 每轮预召回缓存:react 多步共享,不重复 embedding
        self._turn_item_ctx = None   # (item_id, 商品块) 每轮缓存:同商品不重复请求 hmdp
        self._turn_skill_ctx = None   # (skill_name, instructions) 本轮预加载的技能流程
        self._turn_qu = None       # 查询理解结果(orchestrator 每轮注入;引擎独立运行时 None=老行为)
        # L3①:orchestrator 每轮注入的 KB 并发预取结果——(用于预取的 query, rows, backend)。
        # 只有当 _build_messages 里最终要用的检索 query 与预取时的 query **完全相同**才会被
        # 复用(见下方 _build_messages);不同则原地丢弃,回退成一次新的现场检索,不冒充。
        self._turn_kb_prefetch = None
        # L3③:本轮进度阶段单调门(见 app/agent/progress.py ProgressStageGate)。
        # 每轮 chat() 开始时 reset 一次,防止上一轮的阶段位置残留误判本轮。
        from app.agent.progress import ProgressStageGate
        self._turn_progress_gate = ProgressStageGate()
        # L3③ 并发路径修复(Defect-2,见 set_turn_progress_gate 文档):本轮是否
        # 由 orchestrator 注入了"已经推进过"的门,chat() 开头据此决定是 reset
        # 还是直接沿用——默认 False(裸引擎测试/未经 orchestrator 场景不受影响)。
        self._external_progress_gate_pending = False
        # E1:这一轮是否允许 ReAct 第一步流式吐字给买家——由 streaming.py 在
        # 调 chat() 前注入(见 set_turn_stream_eligible),已经把"输出护栏是否
        # 含改写类 guard"这件事在生成前判完。引擎本身拿不到 guard_pipeline，
        # 默认 False(与既有裸 agent 测试/未注入场景保持"不流式"的老行为)。
        self._turn_stream_eligible = False
        self.history_threshold = settings.history_threshold
        self.history_keep_recent = settings.history_keep_recent
        self.max_react_steps = settings.max_react_steps

        self.tool_manager = ToolManager(
            use_mcp=settings.mcp_enabled,
            mcp_server_url=settings.mcp_server_url,
        )

        # 后台高频记忆调用专用 client:短超时零重试,快速失败(防容错层重试累积卡死后台线程)
        from app.observability.langfuse_client import make_openai_client
        _bg_client = make_openai_client(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.memory_bg_timeout_s,
            max_retries=0,
        )
        from app.agent.memory import MemoryManager
        self.memory_manager = MemoryManager(
            client=self.client,
            model=self.model,
            user_id=self.user_id,
            memory_dir=settings.memory_dir,
            memory_enabled=settings.memory_enabled,
            max_ltm_facts=settings.max_ltm_facts,
            ltm_curation=settings.memory_curation_enabled,
            bg_client=_bg_client,
        )

        if settings.memory_enabled:
            from app.agent.tools.memory_tool import set_memory_manager
            set_memory_manager(self.memory_manager)

        from app.agent.skills import SkillManager
        self.skill_manager = SkillManager(
            skills_dir=settings.skills_dir,
            enabled=settings.skills_enabled,
        )
        if settings.skills_enabled:
            from app.agent.tools.skill_tool import set_skill_manager
            set_skill_manager(self.skill_manager)

        self.raw_messages: list[dict] = []
        self.summary: Optional[str] = None
        self._status: str = "complete"   # R2 checkpoint:complete / in_flight
        self._step_seq: int = 0          # 本回合已 checkpoint 的工具步数
        self._pending = None             # R3 挂起待确认动作(PendingAction),随会话状态持久化

        # 事件发射器：默认 None（走控制台打印）；服务层可替换为队列写入等
        self.event_sink: Optional[Callable[[dict], None]] = None

        self._reply_pipeline = ReplyPipeline()

        self.store = get_session_store()
        loaded = self.store.load(self.session_path)
        if loaded:
            self.summary = loaded.get("summary")
            self.raw_messages = loaded.get("messages", [])
            if loaded.get("short_term_memory"):
                self.memory_manager.restore_stm(loaded["short_term_memory"])
            # R2 恢复:上次回合被中断(in_flight)→ 修复可能的孤儿 tool_call/结果,持久历史保持合法
            self._status = loaded.get("status", "complete")
            self._step_seq = loaded.get("step_seq", 0)
            if loaded.get("pending"):       # R3:恢复挂起动作(确认前重启也能续)
                from app.agent.pending import PendingAction
                self._pending = PendingAction.from_dict(loaded["pending"])
            if self._status == "in_flight":
                self.raw_messages = sanitize_tool_pairs(self.raw_messages)
                self._status = "complete"   # 已修复,视为可继续

    @property
    def history_size(self) -> int:
        return len(self.raw_messages)

    def set_turn_understanding(self, qu) -> None:
        """orchestrator 每轮注入查询理解结果(QueryUnderstanding);None=退回默认行为。"""
        self._turn_qu = qu

    def set_turn_kb_prefetch_future(self, query: str, future) -> None:
        """L3①/R1:orchestrator 提交的 KB 并发预取任务——`future` 是那次检索的
        concurrent.futures.Future(此刻可能还没跑完),`query` 是它**实际用来
        检索的那个 query**(见 MultiAgentOrchestrator._prefetch_query:通常是
        买家原句;短的指代型追问会被零 LLM 地拼上最近一条买家话)。

        存的是 Future 本身,不在这里阻塞等结果——真正调用 `future.result()`
        (可能阻塞)推迟到 `_build_messages` 真正需要检索结果的那一刻。

        **R1 起的复用契约(与之前不同,改了要连这段一起改)**:预取一旦提交,
        它就是本轮**唯一一次**检索。`_build_messages` 只按查询理解的 `need_kb`
        决定用还是丢:
          - need_kb=True  → 直接复用这批行(哪怕 QU 把 kb_query 改写成了别的
            句子,也不再为改写后的 query 补一次阻塞检索);若预取时的 query 与
            QU 最终的 kb_query 不同,发一条 `kb_prefetch_reused` 事件如实记录
            "注入的知识是按哪个 query 检出来的",不静默替换。
          - need_kb=False → 丢弃,**绝不注入**(门控语义优先于这次优化),并发
            一条 `kb_prefetch_discarded` 事件留痕。
          - Future 结果为 None(预取本身失败/早退)→ 本轮无知识注入,同样发
            `kb_prefetch_discarded` 留痕,**不**回退现场补检索。
        丢弃时不 cancel,让它在后台自然跑完退出(不影响正确性)。

        为什么不再按"kb_query 与预取 query 逐字节相同"才复用:那个条件实际
        几乎永不成立(QU 几乎总会改写措辞),后果是每轮**两次**阻塞检索——
        预取那次白算,买家还要再等一次完整的 ApeRAG 往返(实测这一段占首字
        延迟 11.8s)。改写带来的召回差异是真实的但有界,且已用零 LLM 的
        `_prefetch_query` 上下文补全把差距最大的那一类(指代型追问)补回来。
        完整的实测数据与取舍论证见 `.superpowers/sdd/r1-report.md`。"""
        self._turn_kb_prefetch = (query, future)

    def set_turn_progress_gate(self, gate) -> None:
        """L3③ 并发路径修复(Defect-2):orchestrator.chat() 在真正调用
        engine.chat() **之前**,已经用这把门发过"understanding"/(并发预取
        提交成功时)"retrieving" 预告——旧写法那两条预告调的是模块级裸函数
        `app.agent.progress.emit_progress(event_sink, ...)`,完全不经任何门,
        跟 engine 自己这把 `_turn_progress_gate` 是两套互不知情的计数器。
        这本身就是"并发路径未被覆盖"的真正原因:engine 内部单调(retrieving
        永远先于 generating,由 `_react_loop`/`_build_messages` 的代码结构
        保证)不等于买家看到的**整轮**(跨 orchestrator/engine 两段代码)单调
        ——recall 完成后如果有任何代码路径(包括未来的改动)想在 generating
        已经发出之后再补一条 retrieving,只要它们各自维护自己的计数器,
        谁都无法察觉这是一次倒退。

        本方法把 orchestrator 已经推进过的**同一个**门实例交给 engine,本轮
        `chat()` 直接复用它继续往下走,不再在 chat() 开头 reset 成一把从 -1
        起步、对 orchestrator 那两条预告一无所知的新门。orchestrator 每轮都
        必须显式调用(哪怕这一轮没有并发预取),与 `set_turn_kb_prefetch_
        future`/`clear_turn_kb_prefetch` 同样"每轮显式覆盖,不留旧值"的姿态
        ——`_external_progress_gate_pending` 只在 `chat()` 开头被消费一次,
        不会跨轮残留。"""
        self._turn_progress_gate = gate
        self._external_progress_gate_pending = True

    def _emit_progress(self, stage: str, domain: str | None = None) -> None:
        """L3③:走本引擎既有的 `_emit` 通道发一条 progress 帧(见
        app/agent/progress.py)。经 `_turn_progress_gate` 单调门:阶段号倒退
        的帧会被吞掉,不会让买家看到"退回上一阶段"的假象(见该门的文档)。

        getattr 防御:裸 agent 测试(EcomAgent.__new__,不走 __init__,或直接
        单测 `_react_loop`/`_answer_without_tools` 而不经 `chat()`)可能压根
        没有这个字段——没有就现建一个,与 `_turn_stream_eligible` 等字段的
        既有防御写法同姿态,不因为新增字段炸掉既有测试。"""
        gate = getattr(self, "_turn_progress_gate", None)
        if gate is None:
            from app.agent.progress import ProgressStageGate
            gate = ProgressStageGate()
            self._turn_progress_gate = gate
        gate.emit(self._emit, stage, domain)

    def clear_turn_kb_prefetch(self) -> None:
        """orchestrator 在"本轮没有提交预取"的每一条分支(关并发开关/QU 关闭/
        原句过短/总开关关)都必须显式调用这个方法,而不是放着不管。

        R1 之后这条纪律比以前更要紧:复用不再要求"query 逐字节相同",上一轮
        残留的 (query, Future) 会被本轮**无条件**当成自己的检索结果用掉——
        以前那道字符串相等的门顺带挡住了一部分跨轮泄漏,现在它没了,唯一的
        防线就是 orchestrator 每轮显式覆盖或清空(与 memory 工具 manager 每轮
        刷新是同一类教训)。"""
        self._turn_kb_prefetch = None

    def set_turn_stream_eligible(self, eligible: bool) -> None:
        """E1b:streaming.py 每轮在调 chat() 前注入——这一轮的输出护栏能不能
        被证明是"局部脱敏"(见 app/guardrails/pipeline.py
        `local_redaction_holdback`)。引擎本身不持有 guard_pipeline，这条
        判断天然只能由上层(streaming.py)做完再告诉它；引擎这边只再叠加一条
        它自己知道、上层不知道的条件——本回合是否已经执行过工具(见
        `_can_stream_step`)。"""
        self._turn_stream_eligible = bool(eligible)

    def chat(self, user_input: str) -> CustomerServiceResponse:
        """处理用户输入：ReAct 循环 → 结构化提取 → 返回结果"""
        set_current_session(self.session_id)
        from app.agent.runtime_context import set_current_user
        set_current_user(self.user_id)
        # 每轮刷新记忆工具的当前 manager:多 agent 并存时防 recall_user_memory 串户
        if settings.memory_enabled:
            from app.agent.tools.memory_tool import set_memory_manager
            set_memory_manager(self.memory_manager)
        self.raw_messages.append({"role": "user", "content": user_input})
        self._step_seq = 0
        self._turn_recall = None   # 新一轮:召回缓存作废,按本轮问题重检索
        self._turn_item_ctx = None
        self._turn_skill_ctx = None
        # 新一轮:阶段单调门归零,不带着上一轮的阶段位置。getattr 防御:裸 agent
        # 测试(EcomAgent.__new__,不走 __init__)没有这个字段——没有就现建一个,
        # 与 _turn_stream_eligible 等字段的既有防御写法同姿态。
        # L3③ 并发路径修复(Defect-2):若 orchestrator 本轮已经通过
        # set_turn_progress_gate 注入了一把"已经推进过"的门(见该方法文档),
        # 这里绝不能 reset 它——reset 会把 orchestrator 刚发出的 understanding/
        # retrieving 预告从这把门的记忆里抹掉,让 engine 后续的阶段号从 -1
        # 重新起步,回到"engine 内部单调、但整轮不单调"的老问题。只消费一次
        # 标记,不跨轮残留(与 _turn_kb_prefetch 每轮显式覆盖同姿态)。
        if getattr(self, "_external_progress_gate_pending", False):
            self._external_progress_gate_pending = False
        elif getattr(self, "_turn_progress_gate", None) is None:
            from app.agent.progress import ProgressStageGate
            self._turn_progress_gate = ProgressStageGate()
        else:
            self._turn_progress_gate.reset()
        # 注意:_turn_kb_prefetch 不在这里清——orchestrator.chat() 在调 engine.chat()
        # **之前**就已经调用 set_turn_kb_prefetch/clear_turn_kb_prefetch 注入或清空本轮
        # 结果(与 set_turn_understanding 同样的时序),这里若清空反而会把刚注入的值
        # 抹掉。跨轮残留风险由 orchestrator 侧兜底:它在"本轮不预取"的每一条分支都
        # 显式调用 clear_turn_kb_prefetch(而不是不闻不问留着上一轮的值),裸引擎
        # (不经 orchestrator 直接测试/使用)则该字段恒为 None,同样不存在残留。
        from app.agent.skills.execution_trace import SkillTurn
        self._skill_turn = SkillTurn()   # G2:新一轮 skill 执行轨迹(旁路埋点)
        self._checkpoint("in_flight")   # 回合开始:持久化用户消息 + 标记进行中

        # FAQ 语义缓存秒答(文档2.5缓存预热):QU 判定需检索的政策类问题先查预热缓存,
        # 命中=零 LLM 直答(毫秒级);未命中/关闭/失败走正常流程。会话落账与持久化照常。
        if (settings.faq_cache_enabled and self._turn_qu is not None
                and self._turn_qu.need_kb):
            from app.agent.faq_cache import get_faq_cache, FaqLookupOutcome
            from app.agent.rag.errors import EmbeddingIndexMismatchError
            # get_faq_cache() 故意放在下面的容错 try 之外:单例首次构造时若发现
            # 持久化缓存的 embedding 模型/维度与当前配置不一致,会抛
            # EmbeddingIndexMismatchError——这是部署错误而不是瞬时故障,必须
            # 让它明确失败(参见 app/agent/faq_cache.py 顶部说明),不能被下面
            # "单次 embedding 调用失败=未命中"的常规容错一起吞掉。
            cache = get_faq_cache()
            try:
                outcome = cache.lookup_with_state(self._turn_qu.kb_query or user_input)
            except EmbeddingIndexMismatchError:
                raise
            except Exception as exc:   # 容错红线:查询过程的意外异常=不可用,绝不打断主流程
                outcome = FaqLookupOutcome(state="unavailable", error=str(exc)[:200])
            # 三态埋点(W1 L1):hit/miss/unavailable 每轮都发一条,trace 里才能
            # 区分"真的没匹配到"与"这一轮 embedding 调用失败"——过去只在命中时
            # 才发事件,后两者在 trace 里完全一样,是整个子系统坏了很久没人
            # 发现的直接原因。
            self._emit({"type": "faq_cache", "state": outcome.state,
                        **({"matched": outcome.hit["question"], "score": outcome.hit["score"]}
                           if outcome.state == "hit" else {}),
                        **({"error": outcome.error} if outcome.state == "unavailable" else {})})
            if outcome.state == "hit":
                _hit = outcome.hit
                result = CustomerServiceResponse(
                    intent=IntentType.OTHER, confidence=1.0,
                    reply=_hit["answer"] + "\n(依据《常见问题FAQ》)",
                    requires_human=False, follow_up_question=None)
                self.raw_messages.append(
                    {"role": "assistant", "content": result.model_dump_json()})
                self._status = "complete"
                self.store.save(self.session_path, self._session_state())
                self._write_snapshot()
                # Finding-1修复:两个方法本身是幂等的旁路埋点(开关/异常都
                # fail-soft),在这里调用一次、末尾正常路径调用一次,与下面的
                # 闲聊寒暄零 LLM 分支互斥(每轮只会真正走到其中一个 return,
                # 不会重复落库)。
                self._record_skill_turn(result)
                self._record_turn_signal(result)
                return result

        # L3②(实测发现的真实缺口):闲聊寒暄类"问候/感谢/告别"命中规则快筛后,
        # 查询理解本身零 LLM,但**生成回复**原来仍要走一次完整 ReAct/生成调用——
        # 规则只免了理解那一步,没免生成那一步,买家还是要为一句"你好"全额
        # 付一次生成延迟。这正是 app/hardening/fast_path.py 已经解决过的同一类
        # 问题(同一份规则、同一份已审过的文案),只是那道快路径只挂在 HTTP
        # 入口(app.py),编排器/引擎被其它入口复用时(测试驱动/未来的非 HTTP
        # 调用方)会绕开它。这里复用**同一个** match_fast_path 判定 + 同一份
        # 文案在引擎层再兜一次,不新造一套未经审过的话术,也不放宽到规则表里
        # 其它含糊的"闲聊寒暄"匹配(如确认语气词"好的"/"嗯"——这些没有对应的
        # 安全文案,不强行套用,继续走生成)。
        if (settings.fast_path_enabled and self._turn_qu is not None
                and self._turn_qu.source == "rule" and self._turn_qu.intent == "闲聊寒暄"):
            from app.hardening.fast_path import match_fast_path
            fp = match_fast_path(user_input)
            if fp is not None:
                result = CustomerServiceResponse(
                    intent=self._intent_from_qu(), confidence=self._confidence_from_qu(),
                    reply=fp["reply"], requires_human=False, follow_up_question=None)
                self.raw_messages.append(
                    {"role": "assistant", "content": result.model_dump_json()})
                self._status = "complete"
                self.store.save(self.session_path, self._session_state())
                self._write_snapshot()
                self._record_skill_turn(result)
                self._record_turn_signal(result)
                return result

        self._preload_skill(user_input)

        # L3③ Defect-1 修复:"正在为您生成回复"这条进度**不在这里发**——这里
        # 只是"决定要不要走 ReAct 循环",messages 还没组装,而组装 messages
        # (_build_messages,在 _react_loop 第一步内)本身可能触发一次真实的
        # KB 检索(_emit_progress("retrieving", ...)),那次检索在事实上*先于*
        # 生成发生。之前在这里(ReAct 循环开始前)就无条件发"正在生成",实测
        # 出现过"generating 在 retrieving 之前到达"的倒序帧(检索其实还没做完,
        # 却已经说"正在生成")——比不显示还糟的那种"倒退的进度提示"。现在把
        # 这条提示挪到 _react_loop 第一步、_build_messages() **返回之后**才发
        # (见 _react_loop),用代码结构本身保证时序正确,不再是猜时机。
        # stage 事件:供观测层(自研 tracer/Langfuse 桥)组装阶段 span 树
        self._emit({"type": "stage", "status": "start", "name": "react"})
        try:
            final_text = self._react_loop()
        finally:
            self._emit({"type": "stage", "status": "end", "name": "react"})

        # H1:出话草稿 → 评估/重写/润色流水线(仅复杂轮;简单轮/关开关时 run() 内部直接原样返回)
        final_text = self._reply_pipeline.run(
            self.client, self.model, user_input, final_text,
            self._grounding_context(), self._step_seq > 0, self._emit,
        )

        self._check_internal_leak(final_text)

        result = self._extract_structured_response(final_text)

        self.memory_manager.update_short_term(self.raw_messages[-6:],
                                              all_messages=self.raw_messages)

        self.raw_messages.append(
            {"role": "assistant", "content": result.model_dump_json()}
        )

        _budget = (settings.context_window_tokens - settings.max_output_tokens
                   - settings.context_safety_buffer)
        if estimate_tokens(self._build_messages()) > _budget:
            self._compress_history()

        self._status = "complete"
        self.store.save(self.session_path, self._session_state())   # 回合结束:完整落盘(必落)
        self._write_snapshot()
        self._record_skill_turn(result)
        self._record_turn_signal(result)
        return result

    def _record_turn_signal(self, result: CustomerServiceResponse) -> None:
        """N2:旁路埋点,按每一轮记录意图/情绪信号(供参谋统计与告警)。

        与 _record_skill_turn 同姿态:开关关闭或落库失败都直接返回,绝不影响
        回复。与 skill_traces 分表而单独落 turn_signals——skill_traces 只在
        本轮加载过 skill 时才有行,而情绪要按每一轮统计,塞进去会让分母失真。
        情绪取 self._turn_qu;引擎独立运行(无 QU 注入)时按 neutral 记,不漏字段。
        """
        if not settings.emotion_trace_enabled:
            return
        qu = self._turn_qu
        try:
            from app.db import get_db
            get_db().record_turn_signal(
                session_id=self.session_id, user_id=self.user_id,
                intent=getattr(qu, "intent", "其他") if qu is not None else "其他",
                emotion=getattr(qu, "emotion", "neutral") if qu is not None else "neutral",
                emotion_level=getattr(qu, "emotion_level", 0) if qu is not None else 0,
                requires_human=result.requires_human,
            )
        except Exception:  # noqa: BLE001 埋点失败绝不影响本轮回复
            pass

    def _check_internal_leak(self, text: str) -> None:
        """L4:出话检查——回复里若出现 skill 名或内部黑话,发一条观测事件。

        只做"看见",不阻断、不改写回复文本本身(要不要拦/怎么改是下一阶段的
        决策,这一步先让"发生过"这件事可被观测到)。旁路埋点,与
        `_record_skill_turn`/`_record_turn_signal` 同姿态:任何异常都吞掉,
        绝不能因为检测器自己出错而影响本轮回复。
        """
        try:
            from app.agent.jargon_guard import detect_internal_leak
            names = self.skill_manager.skill_names if self.skill_manager else []
            hits = detect_internal_leak(text, names)
            if hits:
                self._emit({"type": "guard", "kind": "internal_leak", "hits": hits})
        except Exception:  # noqa: BLE001 埋点失败绝不影响本轮回复
            pass

    def _record_skill_turn(self, result: CustomerServiceResponse) -> None:
        """G2:本轮若加载过 skill,把执行轨迹落库(供 G3 失败采集 / G4 门禁分析)。

        旁路埋点:开关关闭、未加载 skill、或落库失败都直接返回,绝不影响回复。
        """
        if not settings.skill_trace_enabled:
            return
        turn = getattr(self, "_skill_turn", None)
        if turn is None or not turn.has_skill:
            return
        try:
            from app.db import get_db
            get_db().record_skill_trace(
                session_id=self.session_id, user_id=self.user_id,
                skill_name=turn.skill_name, tool_calls=turn.tool_calls,
                outcome=turn.outcome(result.requires_human),
                variant=turn.variant, skill_version=turn.skill_version,
                skill_fingerprint=turn.skill_fingerprint,
            )
        except Exception:  # noqa: BLE001 埋点失败绝不影响本轮回复
            pass

    def _session_state(self) -> dict:
        """当前会话状态(交给 SessionStore 持久化)。"""
        return {
            "version": 1,
            "messages": self.raw_messages,
            "summary": self.summary,
            "short_term_memory": self.memory_manager.stm_to_dict(),
            "status": self._status,
            "step_seq": self._step_seq,
            "pending": self._pending.to_dict() if self._pending else None,
        }

    def _checkpoint(self, status: str) -> None:
        """步级 checkpoint:更新 status 并落盘。回合中途崩溃可从最后一次 checkpoint 恢复。"""
        self._status = status
        if settings.checkpoint_enabled:
            self.store.save(self.session_path, self._session_state())

    def reset(self):
        self.raw_messages = []
        self.summary = None
        self.memory_manager.reset_short_term()
        self.store.delete(self.session_path)

    def save(self) -> None:
        self.store.save(self.session_path, self._session_state())
        self._write_snapshot()

    def _write_snapshot(self) -> None:
        """会话冷快照(best-effort):每回合落盘后同步一份到 SQLite,热会话过期后仍可回看。

        不依赖 reaper / 进程内存态——任何会话都有永久副本。session_id 取自
        session_path 的 stem(与 RedisSessionStore 的 key 同源);默认会话名跳过。
        """
        if not settings.session_snapshot_enabled:
            return
        from pathlib import Path
        sid = Path(self.session_path).stem
        if not sid or sid == "session":
            return
        try:
            from app.db import get_db
            get_db().upsert_session_snapshot(sid, self.user_id or "default",
                                             self.raw_messages, self.summary)
        except Exception:
            pass

    def close(self):
        self.memory_manager.consolidate_to_long_term(self.raw_messages, self.summary)
        self.tool_manager.close()

    # 空回复重试兜底文案(重试仍空时用,避免给用户一片空白)
    _EMPTY_REPLY_FALLBACK = (
        "抱歉，我这边刚刚没能生成回复。您可以换个说法再问一次，"
        "或直接告诉我订单号/具体问题，我马上为您处理～"
    )

    def _llm_create(self, messages: list[dict], use_tools: bool):
        """统一的 LLM 调用入口:use_tools 决定是否带 function calling。"""
        kwargs = dict(model=self.model, messages=messages, temperature=self.temperature)
        if use_tools:
            kwargs["tools"] = self.tool_manager.tool_definitions
        return self.client.chat.completions.create(**kwargs)

    def _can_stream_step(self) -> bool:
        """E1b:当前 ReAct 步是否可以流式发起。只看**生成开始前就已知**的
        静态信号，不做任何"猜内容"的预测——为什么不能猜见
        app/guardrails/base.py `OutputGuard` 的分类说明：一个证明不了"只改
        局部"的变换类护栏一旦命中就可能整段改写回复，已经流出去的前缀事后
        没法收回，所以"要不要流式"必须在按下生成键之前就拍板，不能等边生成
        边看内容再决定。

          1) 总开关 `settings.stream_reply_enabled`；
          2) `_turn_stream_eligible`——streaming.py 在调 `chat()` 前已经把
             "这一轮的输出护栏能不能被证明是局部脱敏"判完并注入（引擎本身
             不持有 guard_pipeline，没有能力、也不该在这里重复判断；局部
             脱敏护栏真正的安全网是 streaming.py 里的 IncrementalRedactor，
             这里只需要知道"能不能流"这个二元结论）。

        引擎这边再叠加一条上层不知道、只有引擎自己知道的条件——H1 回复流水
        线（`self._reply_pipeline`）的 `complex_turn` 参数在 chat() 里传的是
        `self._step_seq > 0`，即"本回合是否已经执行过工具"。这个值只会在
        执行工具后递增，从不减少，所以：

          - 当前步开始前 `self._step_seq == 0`（本回合到这一步为止还没执行
            过任何工具）：如果这一步恰好就是终答（不再调用工具），
            `complex_turn` 必然是 False，回复流水线 `run()` 会在最开头直接
            原样返回草稿——不存在"生成完再被 H1 改一遍"的风险，可以流。
            （如果这一步反而调用了工具，`_llm_create_streaming` 检测到
            `tool_calls` 后会自动停发 `reply_delta`，不会误流出过程性文字。）
          - 当前步开始前 `self._step_seq > 0`（更早的某一步已经执行过工具）：
            只要 `settings.reply_pipeline_enabled`（默认开）没关，
            `complex_turn` 必然是 True——回复流水线**一定**会至少跑一次
            真实 LLM 润色（`_run_loop` 收敛路径必经 polish），这正是"生成完
            还要被改写"的风险，跟输出护栏是同一类问题，不能流。只有当运营
            方主动关掉 `reply_pipeline_enabled`（回复流水线总开关）时，
            complex_turn 的值才不再重要（`run()` 直接原样返回草稿），这种
            配置下已经调用过工具的步骤也可以放开流式——这是相对 E1 报告里
            "第 1 步及以后恒非流式"的收窄，不是漏做：默认配置下这条收窄
            不生效（因为 reply_pipeline 默认开），是诊断后的诚实结论，不是
            实现缺口。
        """
        # getattr 防御:裸 agent 测试(EcomAgent.__new__,不走 __init__)可能
        # 压根没设过这个属性——没设等价于"没被上层判定过 eligible",按 False
        # 处理(=不流式),与老行为一致,不因为新增字段炸掉既有测试。
        if not (settings.stream_reply_enabled
                and getattr(self, "_turn_stream_eligible", False)):
            return False
        if self._step_seq == 0:
            return True
        return not settings.reply_pipeline_enabled

    def _llm_create_streaming(self, messages: list[dict]) -> "_StreamedMessage":
        """E1:真流式发起 ReAct 第 0 步生成——token 级增量通过 `reply_delta`
        事件吐给买家（走既有 `self._emit` 通道，tracer/langfuse 桥/SSE 队列
        原样收到，不是另开的第二条通道）。

        若模型这一步实际决定调用工具（delta 带 `tool_calls`），从检测到的
        那一刻起不再发任何 `reply_delta`，只静默把 content/tool_calls 攒
        完，拼成与非流式版本形状一致的 message 对象交回上层——`_parse_tool_
        calls`/工具执行逻辑零改动。

        已知残余风险（记在报告里，不是本任务要堵的那类泄露）：如果模型在
        真正落到 tool_calls 之前先流出几个字的过渡评论（比如"让我查一
        下"），这几个字会被当成 reply_delta 先发出去——跟"输出护栏改写"是
        两类问题：护栏改写是"买家看到了本该被换掉的文本"，这里是"买家看到
        了一句后来被完整回复覆盖的过渡话"，终帧(`reply`)照样会覆盖它。

        首个 `reply_delta` 带 `first: True`，供观测层记"首字时间"。
        """
        kwargs = dict(model=self.model, messages=messages, temperature=self.temperature,
                      tools=self.tool_manager.tool_definitions, stream=True)
        stream = self.client.chat.completions.create(**kwargs)
        content_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        is_tool_call = False
        emitted_first = False
        for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = choices[0].delta
            delta_tool_calls = getattr(delta, "tool_calls", None)
            if delta_tool_calls:
                is_tool_call = True   # 一旦出现 tool_calls,这一步不是终答,后面不再发 reply_delta
                for tcd in delta_tool_calls:
                    idx = getattr(tcd, "index", 0) or 0
                    slot = tool_calls_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if getattr(tcd, "id", None):
                        slot["id"] = tcd.id
                    fn = getattr(tcd, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] += fn.name
                        if getattr(fn, "arguments", None):
                            slot["arguments"] += fn.arguments
            delta_content = getattr(delta, "content", None)
            if delta_content:
                content_parts.append(delta_content)
                if not is_tool_call:
                    event = {"type": "reply_delta", "content": delta_content}
                    if not emitted_first:
                        event["first"] = True
                        emitted_first = True
                    self._emit(event)
        tool_calls = None
        if tool_calls_acc:
            tool_calls = [
                _StreamedToolCall(v["id"], v["name"], v["arguments"])
                for _, v in sorted(tool_calls_acc.items())
            ]
        return _StreamedMessage("".join(content_parts), tool_calls)

    def _answer_without_tools(self) -> str:
        """不带 tools 再问一次,让模型用自然语言直接作答(循环兜底/畸形降级共用)。"""
        response = self._llm_create(self._build_messages(), use_tools=False)
        content = response.choices[0].message.content or ""
        self.raw_messages.append({"role": "assistant", "content": content})
        return content

    @staticmethod
    def _parse_tool_calls(tool_calls) -> tuple[list, bool]:
        """解析 tool_calls;任一参数非法 JSON / 非对象 / 工具名缺失 → 标记 malformed。

        返回 (parsed=[(tc, name, args), ...], malformed)。malformed 时 parsed 不完整,调用方应降级。
        """
        parsed = []
        for tc in tool_calls:
            name = getattr(tc.function, "name", None)
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (ValueError, TypeError):
                args = None
            if not name or not isinstance(args, dict):
                return parsed, True
            parsed.append((tc, name, args))
        return parsed, False

    def _react_loop(self) -> str:
        """ReAct 循环：调用 LLM → 执行工具 → 观察结果 → 重复，直到模型给出最终回答。

        E1b(回复流式化):每一步都问一次 `_can_stream_step()`——原因见该方法
        的 docstring：默认配置下(`reply_pipeline_enabled=True`)只有第 0 步
        能通过(与 E1 报告的行为一致)，但关掉回复流水线总开关后，后续步骤
        （已经调用过工具的那些）也能流，不再是硬编码的"仅 step==0"。
        """
        for step in range(self.max_react_steps):
            messages = self._build_messages()
            if step == 0:
                # L3③ Defect-1 修复:messages 组装完(任何真实检索——见
                # _build_messages 里的 _emit_progress("retrieving", ...)——
                # 已经发生完毕)才说"正在生成",时序由代码顺序保证,不是猜的。
                # 只在第 0 步发一次:同一轮内 messages 里的检索段只算一次
                # (_turn_recall 缓存),后续步骤不会再触发新的检索,也不需要
                # 重复提示"正在生成"。
                self._emit_progress("generating")
            if self._can_stream_step():
                assistant_msg = self._llm_create_streaming(messages)
            else:
                response = self._llm_create(messages, use_tools=True)
                assistant_msg = response.choices[0].message

            if assistant_msg.content:
                self._emit({"type": "thought", "content": assistant_msg.content})

            if not assistant_msg.tool_calls:
                content = assistant_msg.content or ""
                if not content.strip():
                    # Phase 4.1①空回复:不带 tools 重试一次;仍空则给兜底文案,不冒泡空白
                    retry = self._llm_create(messages, use_tools=False)
                    content = (retry.choices[0].message.content or "").strip()
                    if not content:
                        content = self._EMPTY_REPLY_FALLBACK
                    self._emit({"type": "degrade", "reason": "empty_reply"})
                self.raw_messages.append({"role": "assistant", "content": content})
                return content

            # Phase 4.1②畸形工具调用:任一 tool_call 参数非法/工具名缺失 → 降级为无工具自然语言回答
            parsed_calls, malformed = self._parse_tool_calls(assistant_msg.tool_calls)
            if malformed:
                self._emit({"type": "degrade", "reason": "malformed_tool_call"})
                return self._answer_without_tools()

            msg_dict = {"role": "assistant", "content": assistant_msg.content}
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in assistant_msg.tool_calls
            ]
            self.raw_messages.append(msg_dict)

            for tc, func_name, func_args in parsed_calls:
                self._execute_tool_call(tc.id, func_name, func_args)

        return self._answer_without_tools()

    def _workflow_denial(self, name: str, args: dict) -> str | None:
        """G1:本轮已加载的 skill 是否禁止此刻调用该工具(前置步骤/参数未满足)。

        返回 None = 放行;字符串 = 拒绝理由(会被当作工具结果回传给模型)。
        fail-open:守卫判定自身任何异常都放行——绝不能让守卫的 bug 让工具全线不可用。
        """
        turn = getattr(self, "_skill_turn", None)
        if turn is None or not getattr(turn, "skill_name", ""):
            return None            # 本轮没加载 skill → 无 skill 级约束
        manager = getattr(self, "skill_manager", None)
        if manager is None or not getattr(manager, "enabled", False):
            return None
        try:
            from app.agent.skills.workflow import evaluate_guards

            # 一轮内可能加载多个 skill;必须对**每个**都判,否则后加载的 skill
            # 会把前一个的守卫顶掉(先 load process-return 再 load track-order,
            # 退款就不再要求先查单了)。
            names = list(getattr(turn, "loaded_skills", None) or [])
            if not names and turn.skill_name:
                names = [turn.skill_name]
            for loaded_name in names:
                workflow = manager.get_workflow(loaded_name)
                if not workflow:
                    continue
                denial = evaluate_guards(workflow, name, args, turn.tool_calls)
                if denial is not None:
                    return denial
            return None
        except Exception:  # noqa: BLE001 守卫出错=放行,不阻断业务
            return None

    def _execute_tool_call(self, tool_call_id: str, name: str, args: dict) -> str:
        """工具生命周期缝(observe):before(埋点)→ 执行 → after(埋点+挂起观察+写历史)。

        埋点是"观察"——只记录不否决;真正的动作授权在工具内部的 consent 门强制。
        """
        self._emit({"type": "tool_call", "name": name, "args": args})          # before

        # G1 工作流守卫:前置步骤/参数未满足 → 不执行工具,把拒绝当作工具结果回传,
        # 模型据此自行补齐前置步骤(零改动 ReAct 结构,对话不中断)。
        denial = self._workflow_denial(name, args)
        if denial is not None:
            result_str = json.dumps(
                {"success": False, "error": denial, "workflow_guard": True},
                ensure_ascii=False)
            self._emit({"type": "workflow_guard", "name": name, "reason": denial})
            self._emit({"type": "tool_result", "content": result_str})   # 与 tool_call 配对,否则前端工具卡片一直转
            turn = getattr(self, "_skill_turn", None)
            if turn is not None:
                try:
                    turn.note_blocked(name, args, denial)
                except Exception:  # noqa: BLE001
                    pass
            self.raw_messages.append(
                {"role": "tool", "tool_call_id": tool_call_id, "content": result_str})
            self._step_seq += 1
            self._checkpoint("in_flight")
            return result_str

        result_str = self.tool_manager.execute_tool(name, args)
        self._emit({"type": "tool_result", "content": result_str})             # after

        # G2 旁路埋点:记进本轮 skill 轨迹。只观察不否决,异常一律吞掉。
        turn = getattr(self, "_skill_turn", None)
        if turn is not None:
            try:
                turn.note_tool_call(name, result_str, args=args)
            except Exception:  # noqa: BLE001
                pass

        # 记住/清除待确认动作:随会话状态持久化,供确认轮由服务端确定性重放(Phase 4 / R3)
        from app.agent.pending import evaluate_pending
        act, pa = evaluate_pending(name, args, result_str)
        if act == "set":
            self._pending = pa
        elif act == "clear":
            self._pending = None

        self.raw_messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": result_str})
        self._step_seq += 1
        self._checkpoint("in_flight")   # 步级 checkpoint:每个工具步后落盘
        return result_str

    def _grounding_context(self) -> str:
        """取本轮(最近一条 user 之后)的工具真实结果,供评估/重写接地。

        截断教训:每条 500 字会把常见工具结果(如 list_user_orders ~775 字)拦腰
        切断,评估器把草稿里真实存在的内容判为"编造"→ 重写反而把正确回复改坏
        (丢单/编造"信息未同步")。故上限提到可配置的 grounding_result_max_chars
        (默认 2000,覆盖绝大多数工具结果全文;超大结果已被 R 系列落盘留指针),
        并加总预算防多工具轮爆 prompt。
        """
        per_cap = settings.grounding_result_max_chars
        total_cap = settings.grounding_total_max_chars
        collected = []
        total = 0
        for msg in reversed(self.raw_messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "tool":
                piece = (msg.get("content") or "")[:per_cap]
                if total + len(piece) > total_cap:
                    break   # 逆序遍历,优先保住最近的工具结果
                collected.append(piece)
                total += len(piece)
        return "\n".join(reversed(collected))

    # E2:QU(查询理解节点,app/agent/understanding.py)七类中文粗粒度路由标签 →
    # 业务侧 IntentType 细分枚举的映射。两套分类体系本来就不是一一对应,映射
    # 天然有损;拿不准的一律落 OTHER,不硬猜。"政策咨询"额外结合 QU 已经判定
    # 的 domain 再细分一层(见 _intent_from_qu),因为它本身太粗,直接落单一枚举
    # 会把"退货流程"和"优惠券规则"这类完全不同的业务问题混进同一个桶。
    _QU_INTENT_MAP: dict = {
        "投诉": IntentType.COMPLAINT,
        "闲聊寒暄": IntentType.GREETING,
        "商品咨询": IntentType.PRODUCT_CONSULT,
        "订单事务": IntentType.ORDER_QUERY,
        "转人工": IntentType.OTHER,   # 无对应细分枚举;是否转人工由 requires_human 字段承载
        "其他": IntentType.OTHER,
    }

    # confidence 字段的定义(schemas/response.py)是"意图识别的置信度"，不是
    # "回复内容靠不靠谱"。零 LLM 后没有模型自评这个数字了,改用诚实、可解释的
    # 常量:按 QU 判定意图时走的是哪条路径给分——规则命中(确定性正则)最高,
    # LLM 判定次之,QU 自身兜底/未注入 QU(意图来源不明)最低,与
    # settings.hitl_confidence_threshold(默认 0.6)的关系也刻意保留:后两种
    # 情形仍能触发"置信度过低"规则升级,不是形同虚设的常量。
    _QU_SOURCE_CONFIDENCE: dict = {"rule": 0.95, "llm": 0.8, "fallback": 0.5}
    _CONFIDENCE_NO_QU = 0.5   # 引擎独立运行、未注入 QU 时:意图来源不明,与 QU 兜底同档

    # 系统提示词(prompts/customer_service.py)规定,模型"查不到/无把握/超出能力"时
    # 必须使用含"转人工/人工客服"字样的统一兜底话术——这段文字本身已经带着
    # "该转人工"的信号,不需要再额外花一次 LLM 调用去"读出"它。
    _HUMAN_HANDOFF_MARKERS = ("转人工", "人工客服")

    def _intent_from_qu(self) -> IntentType:
        """E2:复用 QU 在生成*之前*就判定好的意图,替代生成之后再问模型一次。

        QU 的分类不会被回复内容污染(它先于 ReAct 循环产生),这一点比旧版
        "模型看着自己刚生成的回复再自评一次"更站得住脚。"""
        qu = getattr(self, "_turn_qu", None)
        if qu is None:
            return IntentType.OTHER
        intent = getattr(qu, "intent", None)
        if intent == "政策咨询":
            domain = getattr(qu, "domain", None)
            if domain == "aftersale":
                return IntentType.RETURN_REQUEST
            if domain == "presale":
                return IntentType.PROMOTION
            return IntentType.OTHER
        return self._QU_INTENT_MAP.get(intent, IntentType.OTHER)

    def _confidence_from_qu(self) -> float:
        qu = getattr(self, "_turn_qu", None)
        if qu is None:
            return self._CONFIDENCE_NO_QU
        source = getattr(qu, "source", None)
        return self._QU_SOURCE_CONFIDENCE.get(source, self._CONFIDENCE_NO_QU)

    @classmethod
    def _requires_human_from_text(cls, text: str) -> bool:
        """回复文案里命中转人工话术即视为需要转人工,信号来源与旧版(读同一段
        文字判断)实质相同,只是不再为"读它"单花一次 LLM 调用。"""
        t = text or ""
        return any(marker in t for marker in cls._HUMAN_HANDOFF_MARKERS)

    def _extract_structured_response(self, text: str) -> CustomerServiceResponse:
        """从本轮已有信号组装结构化元数据（意图/置信度/是否转人工），不再二次调用 LLM。

        E2:原实现在 reply 已经生成之后,把这段文字整段喂给模型再跑一次
        `beta.chat.completions.parse`,只为拿 intent/confidence/requires_human
        三个字段——它自己的 system prompt 还写着"reply 字段直接使用原文，
        不要修改或缩减"，即压根不改回复，纯粹为元数据多付一次 round trip
        (~5s，约占全轮延迟的 10%)。改为零 LLM 派生：
          - intent:复用 QU 已经判定好的意图（_intent_from_qu）。
          - confidence:字段本义是"意图识别置信度"，按 QU 判定路径给诚实常量
            （_confidence_from_qu），不再假装能读出一个"0.87"这种看似精确、
            实际编造的数字。
          - requires_human:HITL 规则层（app/api/streaming.py `_normal_flow`）
            本来就会在这之后按规则重新 OR 一次
            （`requires_human_out = result.requires_human or bool(reasons)`），
            模型自报从来不是权威判定；这里改用零成本的文本关键词命中
            （_requires_human_from_text），不丢系统提示词规定的统一兜底话术
            这一类信号。
          - follow_up_question:不再有模型从文本里抽取的追问句，前端也未消费
            该字段（MetadataChips 未渲染），统一 None，与其它非模型分支
            （blocked/human_request/replay）同口径。

        任何异常都不放行——落到 _extract_structured_fallback，保证这一轮
        永远有结构化结果可用，绝不因为元数据组装本身出错而炸掉整轮回复。
        """
        try:
            return CustomerServiceResponse(
                intent=self._intent_from_qu(),
                confidence=self._confidence_from_qu(),
                reply=text,
                requires_human=self._requires_human_from_text(text),
                follow_up_question=None,
            )
        except Exception:
            return self._extract_structured_fallback(text)

    def _extract_structured_fallback(self, text: str) -> CustomerServiceResponse:
        """E2:等效安全网——原版在 response_format 不被 API 支持时改用 prompt 引导
        JSON 输出（仍是一次 LLM 调用）；现在整条元数据组装路径已不含 LLM 调用，
        这里的"解析失败"改指 _extract_structured_response 自身出错（如 QU 对象
        形状异常），安全网也相应改为零 LLM 的硬编码兜底——不管前面出了什么错，
        这里保证必定返回一个合法的 CustomerServiceResponse，不再有第二条网络请求
        可能失败。"""
        return CustomerServiceResponse(
            intent=IntentType.OTHER,
            confidence=self._CONFIDENCE_NO_QU,
            reply=text,
            requires_human=self._requires_human_from_text(text),
            follow_up_question=None,
        )

    def _preload_skill(self, user_input: str) -> None:
        """确定性预加载:按关键词判定本轮该用哪个 skill,程序化加载并记进本轮轨迹。

        实测模型在自然措辞下从不自己调 load_skill,而守卫是 skill 作用域的——
        不预加载就等于守卫与自进化闭环都不生效。故由服务端判定,不依赖模型自觉。
        fail-soft:开关关闭、无匹配、加载失败、任何异常一律静默跳过(退回原行为)。
        """
        if not settings.skill_preload_enabled:
            return
        mgr = self.skill_manager
        if mgr is None or not getattr(mgr, "enabled", False):
            return
        try:
            from app.agent.skills.matcher import match_skill

            name = match_skill(user_input, mgr.get_catalog())
            if not name:
                return
            loaded = mgr.load_skill(name)
            if not loaded.get("success"):
                return
            variant = loaded.get("variant") or "live"
            self._turn_skill_ctx = (name, loaded.get("instructions") or "")
            turn = getattr(self, "_skill_turn", None)
            if turn is not None:
                turn.note_preloaded(name, variant)
                turn.set_version(loaded.get("version") or 0)   # 加载那一刻的版本,带着走
                turn.set_fingerprint(loaded.get("skill_fingerprint") or "unknown")  # 同上,内容指纹
            self._emit({"type": "skill_preloaded", "name": name, "variant": variant})
        except Exception:  # noqa: BLE001 预加载是增强,失败退回原行为
            pass

    def _build_messages(self) -> list[dict]:
        system_content = self.system_prompt
        if self.skill_manager and self.skill_manager.enabled:
            system_content += self.skill_manager.build_catalog_prompt()

        messages: list[dict] = [
            {"role": "system", "content": system_content}
        ]
        # 当前咨询商品上下文:顾客带商品进客服时,让"这/它/这款"指代消解到该商品并接地介绍。
        # 每轮按 item_id 缓存(避免一轮内多次 _build_messages 重复请求 hmdp);
        # 块的**位置**放到最后一条用户消息之前(见下),让当前商品在 recency 上压过历史里聊过的旧商品。
        product_block = None
        from app.agent.runtime_context import get_current_item
        item_id = get_current_item()
        if item_id:
            if self._turn_item_ctx is None or self._turn_item_ctx[0] != item_id:
                from app.agent.product_context import fetch_product_context
                self._turn_item_ctx = (item_id, fetch_product_context(item_id))
            product_block = self._turn_item_ctx[1]
        last_user = next(
            (m.get("content") for m in reversed(self.raw_messages)
             if m.get("role") == "user"),
            None,
        )
        # 统一召回层:profile/LTM/STM/KB 四源一次装配(存储分离、召回统一)。
        # 检索门控与查询改写来自上游查询理解节点(qu);每轮缓存防重复检索。
        from app.agent.recall.service import build_recall_sections
        if self._turn_recall is None or self._turn_recall[0] != last_user:
            qu = self._turn_qu
            include_kb = qu.need_kb if qu is not None else True
            recall_query = (qu.kb_query if qu is not None and qu.kb_query else last_user)
            domain = qu.domain if qu is not None else None
            # L3①/R1:orchestrator 并发预取的 KB 检索 Future。**契约见
            # set_turn_kb_prefetch_future 的文档**——预取一旦提交,它就是本轮
            # 唯一一次检索,这里只按 need_kb 决定用还是丢,不再为"QU 把 query
            # 改写过"补一次阻塞检索(那正是"一轮两次检索、首字多等一整个
            # ApeRAG 往返"的根因)。
            # 只在这一刻才真正阻塞等待(`.result()`)——到这里之前 FAQ 缓存
            # 查询/技能预加载等已经花掉了一些挂钟时间,这段时间与并发检索
            # 天然重叠,真正需要等待的窗口因此被压缩,而不是从头到尾白等。
            _pf = self._turn_kb_prefetch
            kb_prefetch = None
            prefetch_failed = False
            if _pf is not None and not include_kb:
                # 门控说本轮不需要知识(闲聊/纯订单操作):预取的行**绝不注入**。
                # 门控语义优先于这次并发优化——这条是任务的硬约束,不是偏好。
                # 丢弃必须留痕(fail-soft 留痕约束):正因为过去丢弃是静默的,
                # "复用条件几乎永不成立"这个缺陷才活到今天没人发现。
                self._emit({"type": "kb_prefetch_discarded", "reason": "need_kb_false",
                            "intent": (qu.intent if qu is not None else None),
                            "prefetch_query": _pf[0]})
            elif _pf is not None:
                try:
                    fetched = _pf[1].result()
                except Exception:
                    fetched = None   # Future 里的任务已自行 fail-soft;这里再兜一层防御
                if fetched is not None:
                    kb_prefetch = fetched   # (rows, backend[, meta])
                    if _pf[0] != recall_query:
                        # 复用了"按预取 query 检出来的行",而本轮 QU 最终认定的
                        # 检索 query 是另一个——不静默替换,如实记一条,Langfuse
                        # 里能直接看到"这轮注入的知识是按哪个 query 检出来的"。
                        self._emit({"type": "kb_prefetch_reused",
                                    "prefetch_query": _pf[0],
                                    "final_query": recall_query})
                else:
                    # 预取失败/早退:本轮无知识注入,**不**回退现场补检索——
                    # 与全局约束"检索失败必须保持非致命:买家仍然拿到回复,
                    # 只是这一轮没有知识注入"一致,也保证了"一轮至多一次检索"。
                    prefetch_failed = True
                    self._emit({"type": "kb_prefetch_discarded",
                                "reason": "prefetch_failed",
                                "prefetch_query": _pf[0]})
            if kb_prefetch is None and include_kb and not prefetch_failed:
                # 没有预取可用(关并发开关 / QU 关闭 / 裸引擎):这一步即将真正
                # 发起一次(本轮唯一一次)KB 检索,如实上报"正在检索"这个阶段
                # ——不是猜,是这行代码接下来确实要做的事。有预取时这条不会发,
                # orchestrator 提交预取时已经发过一条,买家因此只看到一次。
                self._emit_progress("retrieving", domain)
            # prefetch_failed 时用 include_kb=False 调用:让 KB 源直接留空,而不是
            # 在召回层里现场补一次检索(那就是第二次阻塞检索)。返回后把 backend
            # 从 "skipped" 改标成 "unavailable"——门控主动跳过与检索真的失败是
            # 两回事,观测/前端不该把两者混为一谈。
            rr = build_recall_sections(self.memory_manager, recall_query,
                                       include_kb=include_kb and not prefetch_failed,
                                       kb_domain=domain, kb_prefetch=kb_prefetch)
            if prefetch_failed:
                rr.kb_backend = "unavailable"
            self._turn_recall = (last_user, rr)
            # 降级判据抽出来:**命中分支也可能是降级的**——ApeRAG 挂掉、本地索引
            # 兜底顶上时,这一轮有命中(走命中分支)但依据来自本地旧索引而不是
            # 线上知识库。实测就是这个形态:停掉 aperag-api 后仍然 hits=2、
            # backend=local。只在 miss 分支标 degraded 会漏掉**最常见的那一种**
            # (kb_local_fallback_enabled 默认开着)。
            _meta = rr.kb_latency or {}
            _degraded = (_meta.get("outcome") == "unavailable"
                         or bool(_meta.get("fell_back_to_local"))
                         or rr.kb_backend == "unavailable")
            if rr.kb_hits:   # 命中才发正常事件(前端思考面板+tracer 各消费一次)
                self._emit({"type": "recall", "source": "kb", "backend": rr.kb_backend,
                            "query": recall_query, "hits": rr.kb_hits,
                            "degraded": _degraded,
                            **({"outcome": _meta["outcome"]} if _meta.get("outcome") else {})})
            elif qu is not None and not qu.need_kb:
                # 门控跳过:显式发 skipped 事件,门控工作与否前端一眼可见
                self._emit({"type": "recall", "source": "kb",
                            "skipped": True, "reason": qu.intent})
            else:
                # **没命中且不是门控跳过 —— 这一支此前什么都不发,于是"知识库连不上"
                # 在看板上完全看不见。**
                #
                # 实测过这个后果:ApeRAG 容器停了 24 分钟,`aperag_search` 抛
                # ConnectError → fail-soft 返回 None → 召回 0 条,而客服照常回答、
                # 只是答案里**没有任何政策依据**(退货运费之类答的是模型常识)。
                # 买家侧零症状,运维侧零信号。
                #
                # 两种"0 条"必须分开,它们的处置完全相反:
                #   degraded=True  → 知识库连不上/后端不可用 → 去修依赖
                #   degraded=False → 库是通的但这一问没有相关政策 → 可能要补文档
                # 合成一个"召回 0 条"会让前者被读成后者,而前者是故障。
                # 判据优先用 `kb_latency`(即 kb.py 那个 meta)里的 `outcome`——它是
                # **调用侧的事实**("这次 HTTP 到底成没成"),而 `kb_backend` 只是
                # "最终用了哪个后端":ApeRAG 挂掉后回落本地时 backend 会是 "local",
                # 光看它分不出"本来就配 local"和"aperag 挂了兜底顶上"。
                self._emit({"type": "recall", "source": "kb",
                            "backend": rr.kb_backend, "query": recall_query,
                            "hits": [], "miss": True, "degraded": _degraded,
                            **({"outcome": _meta["outcome"]} if _meta.get("outcome") else {})})
            # 任务②③:ApeRAG 调用耗时/结果观测事件——走既有 tracer/Langfuse 通道
            # (self._emit → event_sink → sink() 里同时喂两者,见 app/api/streaming.py),
            # 不新开一条通道。rr.kb_latency 只在 backend=="aperag" 且本轮真的发起过
            # 一次调用时非 None(见 KbRecall.latency 的文档)——无论这次调用是并发
            # 预取(orchestrator 后台线程,结果经 Future 传回)还是现场检索,落这条
            # 事件的动作本身始终发生在这里(_build_messages,主线程、正确的
            # tracer/Langfuse 上下文),不受"谁真正发起了那次 HTTP 调用"影响。
            if rr.kb_latency is not None:
                self._emit({"type": "kb_latency", "backend": rr.kb_backend, **rr.kb_latency})
        messages.extend(self._turn_recall[1].sections)
        if self.summary:
            messages.append(
                {
                    "role": "system",
                    "content": f"以下是此前对话的摘要，用于延续上下文记忆：\n{self.summary}",
                }
            )
        # 本轮预加载的技能流程,与商品块一样插在最后一条用户消息之前(紧邻本轮问题)。
        skill_block = None
        if self._turn_skill_ctx:
            skill_block = (f"【本轮已加载技能:{self._turn_skill_ctx[0]}】\n"
                           f"{self._turn_skill_ctx[1]}")
        # 顺序:技能流程在前、当前商品在后 —— 商品块要紧邻用户消息,保证"这/它"的指代消解
        pre_user_blocks = [b for b in (skill_block, product_block) if b]
        raw = self.raw_messages
        if pre_user_blocks and raw:
            lu = max((i for i, m in enumerate(raw) if m.get("role") == "user"), default=None)
            if lu is not None:
                messages.extend(raw[:lu])
                for block in pre_user_blocks:
                    messages.append({"role": "system", "content": block})
                messages.extend(raw[lu:])
            else:
                messages.extend(raw)
                for block in pre_user_blocks:
                    messages.append({"role": "system", "content": block})
        else:
            messages.extend(raw)
        return sanitize_tool_pairs(messages)   # 送模型前自愈 tool_calls/tool 结果配对

    def _compress_history(self) -> None:
        keep = self.history_keep_recent
        split = len(self.raw_messages) - keep
        while split > 0 and self.raw_messages[split].get("role") in ("tool",):
            split -= 1
        if split <= 0:
            return
        old_messages = self.raw_messages[:split]
        recent = self.raw_messages[split:]

        new_summary = summarize(
            client=self.client,
            model=self.model,
            old_messages=old_messages,
            prev_summary=self.summary,
        )
        self.summary = new_summary
        self.raw_messages = recent
        print(
            f"\n💾 [已压缩 {len(old_messages)} 条老消息 → summary "
            f"({len(new_summary)} 字)]\n"
        )

    def _emit(self, event: dict) -> None:
        """发射一个过程事件。默认打印到控制台；有 event_sink 时交给 sink。"""
        if self.event_sink is not None:
            self.event_sink(event)
        else:
            self._render_to_console(event)

    def _render_to_console(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "thought":
            print(f"\n💭 [思考] {event['content']}")
        elif etype == "tool_call":
            args_str = ", ".join(f"{k}={v!r}" for k, v in event["args"].items())
            print(f"🔧 [调用工具] {event['name']}({args_str})")
        elif etype == "tool_result":
            result = event["content"]
            display = result if len(result) <= 300 else result[:300] + "..."
            print(f"📋 [工具结果] {display}")
