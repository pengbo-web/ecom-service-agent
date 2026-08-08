"""Langfuse 事件桥(v4 SDK):把本项目事件流组装成一条嵌套 trace。

按 Langfuse 最佳实践(docs/observability/best-practices):
- 一轮对话 = 一条 trace,根观察 as_type="agent",名 `invoke_agent 小夕`;
- `propagate_attributes(session_id, user_id)` 归组会话/用户;
- trace input = 用户消息,output = 最终回复(护栏后);
- 工具调用 = as_type="tool" 观察(带 args/结果);LLM 调用由 langfuse.openai
  drop-in 自动上报为 generation——本桥用 `start_as_current_observation`
  维护 OTel 当前上下文,generation 会自动嵌进所处阶段(react/evaluate/...)。

事件协议(与自研 tracer 共用,见 stage 事件):
  {"type":"stage","status":"start"|"end","name":...}   → 嵌套阶段 span
  tool_call/tool_result → tool 观察;route/guard/handoff → 瞬时 span;
  reply → 根输出;metadata → intent 元数据。

铁律:门控 `settings.langfuse_enabled` 默认关;未装/异常一律静默回退,
绝不影响对话主流程。所有 langfuse import 都是惰性的。
"""

from __future__ import annotations

import os
from contextlib import contextmanager

from app.config.settings import settings


def _ensure_env() -> None:
    if settings.langfuse_public_key:
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    if settings.langfuse_secret_key:
        os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)


def langfuse_turn(session_id: str, user_id: str | None, user_input: str):
    """建一轮对话的 Langfuse 上下文;门控关/未装/异常返回 None(调用方跳过)。"""
    if not settings.langfuse_enabled:
        return None
    try:
        _ensure_env()
        from langfuse import get_client
        return _LangfuseTurn(get_client(), session_id, user_id, user_input)
    except Exception:
        return None


@contextmanager
def background_trace(name: str, session_id: str | None = None,
                     user_id: str | None = None, input=None):
    """后台任务(记忆巩固等)的命名根 trace:期间发生的 LLM 调用(drop-in
    generation)自动嵌到该根下,不再以 OpenAI-generation 游离成条。

    门控关/未装/异常时 yield None 且零副作用(调用方无需判空,with 即可)。
    """
    if not settings.langfuse_enabled:
        yield None
        return
    try:
        _ensure_env()
        from langfuse import get_client, propagate_attributes
        root_cm = get_client().start_as_current_observation(
            as_type="span", name=name, input=input,
        )
        root = root_cm.__enter__()
        prop_cm = propagate_attributes(session_id=session_id or None,
                                       user_id=user_id or None)
        prop_cm.__enter__()
    except Exception:
        yield None
        return
    try:
        yield root
    finally:
        for cm in (prop_cm, root_cm):
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass


class _LangfuseTurn:
    """一轮对话的根观察 + 阶段栈。with 进出;on_event 喂事件。全程 best-effort。"""

    def __init__(self, client, session_id: str, user_id: str | None, user_input: str):
        self._client = client
        self._session_id = session_id
        self._user_id = user_id
        self._user_input = user_input
        self._root_cm = None
        self._root = None
        self._prop_cm = None
        self._stage_cms: list = []   # [(context_manager, observation)]

    # -- 生命周期 --------------------------------------------------------
    def __enter__(self):
        try:
            self._root_cm = self._client.start_as_current_observation(
                as_type="agent", name="invoke_agent 小夕", input=self._user_input,
            )
            self._root = self._root_cm.__enter__()
            from langfuse import propagate_attributes
            self._prop_cm = propagate_attributes(
                session_id=self._session_id or None,
                user_id=self._user_id or None,
            )
            self._prop_cm.__enter__()
        except Exception:
            self._root_cm = self._root = self._prop_cm = None
        return self

    def __exit__(self, exc_type, exc, tb):
        # 兜底闭合残留阶段(事件错配也不泄漏上下文),再关属性传播与根观察
        while self._stage_cms:
            cm, _ = self._stage_cms.pop()
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        for cm in (self._prop_cm, self._root_cm):
            try:
                if cm is not None:
                    cm.__exit__(None, None, None)
            except Exception:
                pass
        return False   # 不吞业务异常

    # -- 事件 -------------------------------------------------------------
    def on_event(self, ev: dict) -> None:
        try:
            self._dispatch(ev)
        except Exception:
            pass   # 观测绝不影响主流程

    def _dispatch(self, ev: dict) -> None:
        if self._root is None:
            return
        etype = ev.get("type")
        if etype == "stage":
            if ev.get("status") == "start":
                cm = self._client.start_as_current_observation(
                    as_type="span", name=str(ev.get("name", "stage")),
                )
                obs = cm.__enter__()
                self._stage_cms.append((cm, obs))
            elif self._stage_cms:
                cm, _ = self._stage_cms.pop()
                cm.__exit__(None, None, None)
        elif etype == "tool_call":
            cm = self._client.start_as_current_observation(
                as_type="tool", name=f"execute_tool {ev.get('name')}",
                input=ev.get("args"),
            )
            obs = cm.__enter__()
            self._stage_cms.append((cm, obs))
        elif etype == "tool_result":
            if self._stage_cms:
                cm, obs = self._stage_cms.pop()
                try:
                    obs.update(output=ev.get("content"))
                finally:
                    cm.__exit__(None, None, None)
        elif etype == "route":
            obs = self._client.start_observation(
                as_type="span", name="route",
                input={"domain": ev.get("key"), "agent": ev.get("agent")},
            )
            obs.end()
        elif etype in ("guard", "handoff"):
            obs = self._client.start_observation(
                as_type="span", name=(f"guard:{ev.get('guard')}" if etype == "guard" else "handoff"),
                input={k: v for k, v in ev.items() if k != "type"},
            )
            obs.end()
        elif etype == "workflow_guard":
            # 安全动作:守卫拦截了一次被跳过的工作流步骤——这是"拦下了什么",
            # 不是"耗时多久"(拦截本身是瞬时判定),故记成即时观察,不建 span 栈;
            # as_type="guardrail" + level="WARNING" 让它在 Langfuse UI 里醒目区分于
            # 普通 span,不用逐条展开树才发现有拦截发生。
            obs = self._client.start_observation(
                as_type="guardrail", name=f"workflow_guard:{ev.get('name')}",
                input={"name": ev.get("name"), "reason": ev.get("reason")},
                level="WARNING",
            )
            obs.end()
        elif etype == "degrade":
            # 失败信号:模型客户端已经切到备选/降级路径。同样是"发生了没有"的
            # 瞬时事实,没有可归属的时长区间(降级判定本身不耗时,真正耗时的
            # LLM 调用由 drop-in generation 各自记账)——即时观察 + WARNING 醒目。
            obs = self._client.start_observation(
                as_type="span", name="degrade",
                input={"reason": ev.get("reason")}, level="WARNING",
            )
            obs.end()
        elif etype == "skill_preloaded":
            # 确定性预加载:哪个技能被预置,记一次即时观察即可,无 start/end 配对信号。
            obs = self._client.start_observation(
                as_type="span", name="skill_preloaded",
                input={"name": ev.get("name"), "variant": ev.get("variant")},
            )
            obs.end()
        elif etype == "faq_cache":
            # 命中缓存 = 跳过了模型这一步本身就是结论,没有"耗时区间"要展示。
            obs = self._client.start_observation(
                as_type="span", name="faq_cache",
                input={"matched": ev.get("matched"), "score": ev.get("score")},
            )
            obs.end()
        elif etype == "recall":
            # 知识库预召回:协议里只有一条完事事件(命中/跳过二选一),没有配对的
            # start 信号——真实检索耗时已经发生在这条事件被发出之前,伪造一个
            # 起点反而失真,故仍按即时观察记录,用 as_type="retriever" 对应
            # Langfuse 原生的检索语义,input/output 分别记查询与命中结果。
            output = ({"skipped": True, "reason": ev.get("reason")} if ev.get("skipped")
                      else {"hits": ev.get("hits")})
            obs = self._client.start_observation(
                as_type="retriever", name="recall",
                input={"source": ev.get("source"), "query": ev.get("query")},
                output=output,
            )
            obs.end()
        elif etype == "thought":
            # ReAct 的一步中间思考文本:与 tool_call/tool_result 不同,协议里没有
            # 与之配对的起止信号,只是模型这一步顺带带的旁白,记即时观察。
            obs = self._client.start_observation(
                as_type="span", name="thought", output=ev.get("content"),
            )
            obs.end()
        elif etype == "evaluate":
            # 回复流水线的评审判定(ok/issues):判定动作本身瞬时完成,产生它的
            # LLM 调用已被 drop-in generation 单独计时,这里只记判定结论。
            obs = self._client.start_observation(
                as_type="evaluator", name="evaluate", output={"ok": ev.get("ok")},
            )
            obs.end()
        elif etype == "polish":
            # 回复润色完成的旁路标记,同样没有独立起止信号,即时观察。
            obs = self._client.start_observation(as_type="span", name="polish")
            obs.end()
        elif etype == "select":
            # 回复流水线选择下一步(继续评审/终止):一次性决策,记入参与理由。
            obs = self._client.start_observation(
                as_type="span", name="select",
                input={"next": ev.get("next"), "reason": ev.get("reason")},
            )
            obs.end()
        elif etype == "reply_delta" and ev.get("first"):
            # E1(回复流式化):首字时间——即时观察,记一次就够(每块都记会把
            # trace 灌满)。用 Langfuse 原生的时间戳字段而不是塞进 input/output
            # 文本里,这样 UI 能直接对着这条观察本身的 start_time 与根 trace
            # 的 start_time 算出"生成开始到第一块出屏"经过了多久。
            obs = self._client.start_observation(
                as_type="span", name="reply_delta:first",
            )
            obs.end()
        elif etype == "reply":
            self._root.update(output=ev.get("content"))
        elif etype == "metadata":
            self._root.update(metadata={
                "intent": ev.get("intent"),
                "confidence": ev.get("confidence"),
                "requires_human": ev.get("requires_human"),
            })
