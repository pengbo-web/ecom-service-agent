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
        self._turn_qu = None       # 查询理解结果(orchestrator 每轮注入;引擎独立运行时 None=老行为)
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
            self.summary = loaded["summary"]
            self.raw_messages = loaded["messages"]
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
        from app.agent.skills.execution_trace import SkillTurn
        self._skill_turn = SkillTurn()   # G2:新一轮 skill 执行轨迹(旁路埋点)
        self._checkpoint("in_flight")   # 回合开始:持久化用户消息 + 标记进行中

        # FAQ 语义缓存秒答(文档2.5缓存预热):QU 判定需检索的政策类问题先查预热缓存,
        # 命中=零 LLM 直答(毫秒级);未命中/关闭/失败走正常流程。会话落账与持久化照常。
        if (settings.faq_cache_enabled and self._turn_qu is not None
                and self._turn_qu.need_kb):
            from app.agent.faq_cache import get_faq_cache
            try:
                _hit = get_faq_cache().lookup(self._turn_qu.kb_query or user_input)
            except Exception:      # 容错红线:缓存任何异常(如坏emb条目)=未命中,绝不打断主流程
                _hit = None
            if _hit is not None:
                self._emit({"type": "faq_cache", "matched": _hit["question"],
                            "score": _hit["score"]})
                result = CustomerServiceResponse(
                    intent=IntentType.OTHER, confidence=1.0,
                    reply=_hit["answer"] + "\n(依据《常见问题FAQ》)",
                    requires_human=False, follow_up_question=None)
                self.raw_messages.append(
                    {"role": "assistant", "content": result.model_dump_json()})
                self._status = "complete"
                self.store.save(self.session_path, self._session_state())
                self._write_snapshot()
                return result

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
        return result

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
        """ReAct 循环：调用 LLM → 执行工具 → 观察结果 → 重复，直到模型给出最终回答。"""
        for step in range(self.max_react_steps):
            messages = self._build_messages()
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

    def _execute_tool_call(self, tool_call_id: str, name: str, args: dict) -> str:
        """工具生命周期缝(observe):before(埋点)→ 执行 → after(埋点+挂起观察+写历史)。

        埋点是"观察"——只记录不否决;真正的动作授权在工具内部的 consent 门强制。
        """
        self._emit({"type": "tool_call", "name": name, "args": args})          # before
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

    def _extract_structured_response(self, text: str) -> CustomerServiceResponse:
        """从最终文本中提取结构化元数据（意图、置信度等）。"""
        try:
            response = self.client.beta.chat.completions.parse(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "基于以下客服回复内容，提取结构化信息。"
                            "reply 字段直接使用原文，不要修改或缩减。"
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                response_format=CustomerServiceResponse,
            )
            return response.choices[0].message.parsed
        except Exception:
            return self._extract_structured_fallback(text)

    def _extract_structured_fallback(self, text: str) -> CustomerServiceResponse:
        """当 response_format 不被 API 支持时，用 prompt 引导 JSON 输出。"""
        intent_values = ", ".join(f'"{e.value}"' for e in IntentType)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "基于以下客服回复内容，提取结构化信息并输出 JSON。\n"
                        "reply 字段直接使用原文，不要修改或缩减。\n\n"
                        "必须严格按照以下 JSON 格式输出（不要加 markdown 代码块）：\n"
                        "{\n"
                        f'  "intent": <从以下选择: {intent_values}>,\n'
                        '  "confidence": <0.0到1.0的浮点数>,\n'
                        '  "reply": <原文回复内容>,\n'
                        '  "requires_human": <true或false>,\n'
                        '  "follow_up_question": <追问问题或null>\n'
                        "}"
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.0,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return CustomerServiceResponse.model_validate_json(raw)

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
            rr = build_recall_sections(self.memory_manager, recall_query,
                                       include_kb=include_kb,
                                       kb_domain=(qu.domain if qu is not None else None))
            self._turn_recall = (last_user, rr)
            if rr.kb_hits:   # 命中才发正常事件(前端思考面板+tracer 各消费一次)
                self._emit({"type": "recall", "source": "kb", "backend": rr.kb_backend,
                            "query": recall_query, "hits": rr.kb_hits})
            elif qu is not None and not qu.need_kb:
                # 门控跳过:显式发 skipped 事件,门控工作与否前端一眼可见
                self._emit({"type": "recall", "source": "kb",
                            "skipped": True, "reason": qu.intent})
        messages.extend(self._turn_recall[1].sections)
        if self.summary:
            messages.append(
                {
                    "role": "system",
                    "content": f"以下是此前对话的摘要，用于延续上下文记忆：\n{self.summary}",
                }
            )
        # 当前商品块插到最后一条用户消息之前(紧邻本轮问题),recency 压过历史里讨论过的其它商品
        raw = self.raw_messages
        if product_block and raw:
            lu = max((i for i, m in enumerate(raw) if m.get("role") == "user"), default=None)
            if lu is not None:
                messages.extend(raw[:lu])
                messages.append({"role": "system", "content": product_block})
                messages.extend(raw[lu:])
            else:
                messages.extend(raw)
                messages.append({"role": "system", "content": product_block})
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
