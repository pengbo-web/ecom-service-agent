"""Multi-Agent 编排器:Router → 领域画像 → 同一个硬化引擎(EcomAgent)。

生产最主流的"薄编排"形态:不是 N 个各自带一套 ReAct 循环的子 Agent,而是
**同一个经过 Phase 1–6 硬化的引擎**,按路由结果切换"画像"(专属 system prompt + 工具子集)。
好处:复用全部硬化(空回复/畸形降级、落盘指针、consent 门、事件流、记忆/持久化),
延迟/成本低,加新领域只是加一份画像。对外接口与 EcomAgent 一致。
"""

from pathlib import Path
from typing import Optional

from app.config.settings import settings
from app.multi_agent.agents import AGENT_CONFIGS
from app.multi_agent.router import DEFAULT_AGENT, Router
from app.agent.tools.manager import ToolManager


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

        # 每个画像 = 专属 prompt + 工具子集(独立 ToolManager,仅暴露该领域允许的工具)
        self.profiles: dict[str, dict] = {}
        for key, cfg in AGENT_CONFIGS.items():
            self.profiles[key] = {
                "name": cfg["name"],
                "prompt": cfg["prompt"],
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

    def chat(self, user_input: str):
        # 统一查询理解(默认):一次调用出 domain/intent/need_kb/kb_query,
        # 替代独立路由;关开关=回退老 Router(每轮必检索,无门控无改写)
        if settings.query_understanding_enabled:
            from app.agent import understanding
            # 用 self.client(streaming 层每轮注入的 TracingClient):QU 调用进当前 trace,
            # token/延迟完整入账;engine.client 在下面 :81 才被覆盖,用它会漏记首轮
            qu = understanding.understand(user_input, self.engine.raw_messages,
                                          self.client, self.engine.model)
            key = qu.domain or self._last_key or DEFAULT_AGENT
        else:
            qu = None
            key = self.router.route(user_input, self.engine.raw_messages)
        self._last_key = key
        self.engine.set_turn_understanding(qu)
        profile = self.profiles.get(key) or next(iter(self.profiles.values()))
        if self.event_sink:
            event = {"type": "route", "agent": profile["name"], "key": key}
            if qu is not None:
                event.update(intent=qu.intent, need_kb=qu.need_kb, source=qu.source)
            self.event_sink(event)
        # 切画像:同一硬化引擎,换 prompt + 工具子集;透传 event_sink/client(含 tracer 包装)
        self.engine.system_prompt = profile["prompt"]
        self.engine.tool_manager = profile["tool_manager"]
        self.engine.event_sink = self.event_sink
        self.engine.client = self.client
        return self.engine.chat(user_input)

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
        for p in self.profiles.values():
            p["tool_manager"].close()
