import json
from typing import Callable, Optional

from openai import OpenAI

from app.agent.storage import delete_session, load_session, save_session
from app.agent.summarizer import summarize
from app.config.settings import settings
from app.prompts.customer_service import SYSTEM_PROMPT
from app.schemas.response import CustomerServiceResponse, IntentType
from app.agent.tools.manager import ToolManager
from app.agent.history_utils import estimate_tokens, sanitize_tool_pairs
from app.agent.tools.bargain import set_current_session


class EcomAgent:
    """电商客服 Agent —— 第八期：Skill 可复用能力模块"""

    def __init__(self, session_path: Optional[str] = None, session_id: Optional[str] = None):
        # 模型容错:启用时用主备熔断代理(memory/summarizer 复用本 client 自动继承)
        if settings.resilience_enabled:
            from app.resilience.factory import make_resilient_client
            self.client = make_resilient_client()
        else:
            self.client = OpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
            )
        self.model = settings.model_name
        self.temperature = settings.temperature
        self.session_path = session_path or settings.session_path
        self.session_id = session_id
        self.history_threshold = settings.history_threshold
        self.history_keep_recent = settings.history_keep_recent
        self.max_react_steps = settings.max_react_steps

        self.tool_manager = ToolManager(
            use_mcp=settings.mcp_enabled,
            mcp_server_url=settings.mcp_server_url,
        )

        from app.agent.memory import MemoryManager
        self.memory_manager = MemoryManager(
            client=self.client,
            model=self.model,
            user_id=settings.memory_user_id,
            memory_dir=settings.memory_dir,
            memory_enabled=settings.memory_enabled,
            max_ltm_facts=settings.max_ltm_facts,
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

        # 事件发射器：默认 None（走控制台打印）；服务层可替换为队列写入等
        self.event_sink: Optional[Callable[[dict], None]] = None

        loaded = load_session(self.session_path)
        if loaded:
            self.summary = loaded["summary"]
            self.raw_messages = loaded["messages"]
            if loaded.get("short_term_memory"):
                self.memory_manager.restore_stm(loaded["short_term_memory"])

    @property
    def history_size(self) -> int:
        return len(self.raw_messages)

    def chat(self, user_input: str) -> CustomerServiceResponse:
        """处理用户输入：ReAct 循环 → 结构化提取 → 返回结果"""
        set_current_session(self.session_id)
        self.raw_messages.append({"role": "user", "content": user_input})

        final_text = self._react_loop()

        result = self._extract_structured_response(final_text)

        self.memory_manager.update_short_term(self.raw_messages[-6:])

        self.raw_messages.append(
            {"role": "assistant", "content": result.model_dump_json()}
        )

        _budget = (settings.context_window_tokens - settings.max_output_tokens
                   - settings.context_safety_buffer)
        if estimate_tokens(self._build_messages()) > _budget:
            self._compress_history()

        save_session(
            self.session_path, self.raw_messages, self.summary,
            short_term_memory=self.memory_manager.stm_to_dict(),
        )
        return result

    def reset(self):
        self.raw_messages = []
        self.summary = None
        self.memory_manager.reset_short_term()
        delete_session(self.session_path)

    def save(self) -> None:
        save_session(
            self.session_path, self.raw_messages, self.summary,
            short_term_memory=self.memory_manager.stm_to_dict(),
        )

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
                self._emit({"type": "tool_call", "name": func_name, "args": func_args})
                result_str = self.tool_manager.execute_tool(func_name, func_args)
                self._emit({"type": "tool_result", "content": result_str})

                # 记住/清除待确认动作:供确认轮由服务端确定性重放(Phase 4)
                from app.agent.pending import observe_tool_result
                observe_tool_result(self.session_id, func_name, func_args, result_str)

                self.raw_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

        return self._answer_without_tools()

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
        system_content = SYSTEM_PROMPT
        if self.skill_manager and self.skill_manager.enabled:
            system_content += self.skill_manager.build_catalog_prompt()

        messages: list[dict] = [
            {"role": "system", "content": system_content}
        ]
        messages.extend(self.memory_manager.build_memory_prompt_sections())
        if self.summary:
            messages.append(
                {
                    "role": "system",
                    "content": f"以下是此前对话的摘要，用于延续上下文记忆：\n{self.summary}",
                }
            )
        messages.extend(self.raw_messages)
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
