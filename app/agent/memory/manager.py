"""记忆管理器：统一管理短期记忆和长期记忆。

EcomAgent 和 MultiAgentOrchestrator 通过此管理器与记忆系统交互。
"""

from __future__ import annotations

from typing import Optional

from openai import OpenAI

from app.agent.memory.long_term import LongTermMemory
from app.agent.memory.short_term import ShortTermMemory
from app.config.settings import settings


class MemoryManager:
    """记忆管理器：统一管理短期记忆和长期记忆。"""

    def __init__(
        self,
        client: OpenAI,
        model: str,
        user_id: str = "default",
        memory_dir: str = "app/sessions/memory",
        memory_enabled: bool = True,
        max_ltm_facts: int = 50,
        ltm_curation: bool = False,
        bg_client: OpenAI | None = None,
    ):
        self.client = client
        # 高频后台记忆调用(STM/中途抽取)专用 client:短超时零重试,快速失败。
        # 不传则回退主 client(测试/向后兼容);会话末 consolidate 仍用主 client(强容错)。
        self._bg_client = bg_client or client
        self.model = model
        self.memory_enabled = memory_enabled

        self.stm = ShortTermMemory()
        self.ltm = LongTermMemory(
            user_id=user_id,
            memory_dir=memory_dir,
            max_facts=max_ltm_facts,
            curate_enabled=ltm_curation,
        )

        if self.memory_enabled:
            self.ltm.load()

        import threading
        self._turn_count = 0
        self._extract_cursor = 0            # N2:已抽取到的消息游标
        self._bg_lock = threading.Lock()    # 串行化后台记忆任务(STM/中途抽取/会话末巩固)
        self._bg_thread = None

    def update_short_term(self, recent_messages: list[dict],
                          all_messages: list[dict] | None = None) -> None:
        """每轮对话后调用:按 stm_update_every_n_turns 节流,按需异步执行。

        节流依据:中间轮次的原始消息本来就在上下文里,摘要不需要每轮刷新;
        实测每轮同步更新一次 LLM 调用 ~15s/500+ tok,是响应时间大头。
        """
        if not self.memory_enabled:
            return
        from app.config.settings import settings as _s
        self._turn_count += 1
        n = max(1, _s.stm_update_every_n_turns)
        due_stm = self._turn_count % n == 0
        m = _s.memory_checkpoint_every_n_turns
        due_ckpt = m > 0 and self._turn_count % m == 0 and all_messages
        if not due_stm and not due_ckpt:
            return

        recent = list(recent_messages)                       # 快照:主线程会继续 append
        full = list(all_messages) if all_messages else []
        try:
            from app.agent.tools.bargain import get_current_session
            session_id = get_current_session() or ""
        except Exception:
            session_id = ""

        def work():
            from app.observability.langfuse_bridge import background_trace
            with self._bg_lock:
                if due_stm:
                    try:
                        with background_trace("update_short_term",
                                              session_id=session_id, user_id=self.ltm.user_id):
                            self.stm.update(self._bg_client, self.model, recent)
                    except Exception:
                        pass
                if due_ckpt:
                    self._checkpoint_extract(full, session_id)

        self._run_bg(work)

    def _run_bg(self, fn) -> None:
        from app.config.settings import settings as _s
        if not _s.memory_async_updates:
            fn()
            return
        import threading
        t = threading.Thread(target=fn, daemon=True, name="memory-bg")
        self._bg_thread = t
        t.start()

    def _checkpoint_extract(self, all_messages: list[dict], session_id: str) -> None:
        """长会话中途隐式记忆抽取:只喂游标之后的新消息,完成后推进游标。

        与会话末巩固双通道共享游标——互不重复抽取;重启后游标归 0 会重抽
        旧消息,由策展/内容去重兜底(已知取舍)。调用方已持 _bg_lock。
        """
        segment = all_messages[self._extract_cursor:]
        if not segment:
            return
        try:
            from app.observability.langfuse_bridge import background_trace
            with background_trace("memory_checkpoint",
                                  session_id=session_id, user_id=self.ltm.user_id,
                                  input={"segment_len": len(segment)}):
                self.ltm.extract_and_save(self._bg_client, self.model, segment, None)
            self._extract_cursor = len(all_messages)
        except Exception:
            pass

    def build_memory_prompt_sections(self, query: str | None = None) -> list[dict]:
        """生成所有记忆相关的 system prompt 消息列表。

        query 透传给长期记忆,用于命中优先注入(见 LongTermMemory.build_prompt_section)。
        """
        if not self.memory_enabled:
            return []

        sections = []
        if settings.memory_profile_enabled:
            try:
                from app.agent.memory.profile import get_profile_store

                store = get_profile_store()
                if store is not None:
                    profile_section = store.get(self.ltm.user_id).to_prompt()
                    if profile_section:
                        sections.append({"role": "system", "content": profile_section})
            except Exception:
                pass
        ltm_section = self.ltm.build_prompt_section(query)
        if ltm_section:
            sections.append({"role": "system", "content": ltm_section})
        stm_section = self.stm.build_prompt_section()
        if stm_section:
            sections.append({"role": "system", "content": stm_section})
        return sections

    def consolidate_to_long_term(
        self, messages: list[dict], summary: Optional[str],
    ) -> None:
        """会话结束时，将本次对话的关键事实巩固到长期记忆。"""
        if not self.memory_enabled:
            return
        with self._bg_lock:
            self.ltm.extract_and_save(
                self.client, self.model, messages[self._extract_cursor:], summary,
            )
            self._extract_cursor = len(messages)

    def reset_short_term(self) -> None:
        """重置短期记忆（会话内重置时调用）。"""
        self.stm.reset()

    def reset_all(self) -> None:
        """重置所有记忆（短期+长期）。"""
        self.stm.reset()
        self.ltm.reset()

    def stm_to_dict(self) -> dict:
        return self.stm.to_dict()

    def restore_stm(self, data: dict) -> None:
        self.stm = ShortTermMemory.from_dict(data)
