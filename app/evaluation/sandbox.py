"""评估沙箱：隔离、可复现地重跑测试集，并采集运行全过程（第9期）。

沙箱做三件事：
1. 隔离：每条用例独立的临时 session 文件；关闭记忆读写（否则 default.json 会注入
   prompt 污染评分）；关闭 MCP 只用本地 mock 工具（保证可复现，且让幻觉检测有确定
   的 ground truth）。
2. 插桩：单/多 Agent 都只共享一个 OpenAI client 实例，给它的
   chat.completions.create / beta.chat.completions.parse 打补丁，即可捕获整个会话
   所有 LLM 调用的 token、被请求的工具、延迟——无需改动 chat.py / orchestrator.py。
   工具返回值则通过包裹 ToolManager.execute_tool 采集。
3. 执行：顺序跑完用例的多轮输入，把过程与结果填进 RunTrace 返回。

关键：绝不调用 agent.close()（会触发长期记忆巩固的 LLM 写入，污染且烧钱）；
所有补丁在 finally 中还原。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from app.agent.tools.manager import ToolManager
from app.config.settings import settings
from app.evaluation.dataset import EvalCase
from app.evaluation.trace import LLMCallRecord, RunTrace, ToolObservation


class Sandbox:
    """Agent 评估沙箱：构建隔离环境、插桩采集、跑用例产出 RunTrace。"""

    def __init__(self, mode: str = "single", tmp_root: str | None = None):
        self.mode = mode  # "single" / "multi"
        self.tmp_root = Path(tmp_root) if tmp_root else Path(tempfile.mkdtemp(prefix="eval_sandbox_"))
        self.tmp_root.mkdir(parents=True, exist_ok=True)

    def session_path_for(self, case_id: str) -> str:
        return str(self.tmp_root / f"{case_id}.json")

    @staticmethod
    def buyer_tool_names() -> set:
        """被测买家 Agent 允许看到的工具全集 = 三副买家画像工具子集的并集。

        **从 AGENT_CONFIGS 派生,不手抄名单**:手抄的名单只在"当时没漏"时成立,
        新加一个 B 端工具就会悄悄漏进来(这正是 draft_outreach 踩过的坑)。派生
        之后,新增买家工具自动纳入、新增卖家工具自动排除,名单不会和事实分家。
        """
        from app.multi_agent.agents import AGENT_CONFIGS
        allowed: set = set()
        for cfg in AGENT_CONFIGS.values():
            allowed |= set(cfg["tools"])
        return allowed

    #: 沙箱隔离期间要临时改成的全局 settings(构造 Agent 时 __init__ 直接读全局,
    #: 没有别的注入口)。值必须在 run() 的 finally 里还原,见 _build_agent。
    _ISOLATED_SETTINGS = {"memory_enabled": False, "mcp_enabled": False}

    def _build_agent(self, session_path: str, patches: list[tuple] | None = None):
        """在隔离配置下构建被测 Agent。

        关闭记忆读写与 MCP 保证可复现(agent.__init__ 直接读全局 settings,
        没有构造参数可传)。**这两个写的是进程级单例 `settings`**,所以必须
        跟插桩补丁一样登记进 `patches` 由 run() 的 finally 还原。

        曾经这里是两句裸赋值、从不还原,后果不止是测试互相污染:
        `POST /api/eval/run`(看板上的"运行评估"按钮)是在**服务进程内**起
        线程跑评估的(见 app/evaluation/runner.py),所以在线上点一次评估,
        就会把整个进程的记忆系统与 MCP 永久关掉直到重启——之后所有买家会话
        都不再注入长期/短期记忆,`recall_user_memory` 一律回"记忆系统未启用",
        而且没有任何日志或告警。模块顶部"所有补丁在 finally 中还原"这句话
        当时并不包括这两行。

        `patches=None` 只为兼容直接调用本方法的旧调用点(此时行为同旧版:改了
        不还原);run() 一律传入。
        """
        for name, value in self._ISOLATED_SETTINGS.items():
            if patches is not None:
                patches.append((settings, name, getattr(settings, name)))
            setattr(settings, name, value)

        if self.mode == "multi":
            from app.multi_agent.orchestrator import MultiAgentOrchestrator
            return MultiAgentOrchestrator(session_path=session_path)
        from app.agent.chat import EcomAgent
        agent = EcomAgent(session_path=session_path)
        # 单 Agent 模式下 EcomAgent 自带的 ToolManager 是**全量注册表**,里面含
        # 只属于店主侧的 shop_overview（全店营收）/ product_diagnostics 等只读
        # 工具,以及 draft_outreach —— 后者是一个**写**工具,而且是这条构造路径上
        # 唯一一个不在 admin 鉴权后面的卖家写工具。被测的是买家会话,评测记录会被
        # 完整落进 RunTrace,没有任何理由让它们出现在这里。
        # 换成买家画像并集的受限 ToolManager(与多 Agent 模式下每副画像的做法同
        # 一手法:orchestrator 也是构造后把 engine.tool_manager 换成受限的那个)。
        old_tm = agent.tool_manager
        agent.tool_manager = ToolManager(
            use_mcp=False,                     # 上面刚关掉,显式写出避免被误读
            allowed_tools=self.buyer_tool_names(),
        )
        try:
            old_tm.close()                     # 换掉的那个要关,否则本身就是泄漏
        except Exception:  # noqa: BLE001 关不掉不该让整条用例跑不起来
            pass
        return agent

    def run(self, case: EvalCase) -> RunTrace:
        """跑一条用例，返回采集到的运行轨迹。"""
        trace = RunTrace(case_id=case.id, turns=list(case.turns))
        session_path = self.session_path_for(case.id)

        agent = None
        patches: list[tuple] = []  # (obj, attr, original) 供还原
        try:
            agent = self._build_agent(session_path, patches)
            self._instrument(agent, trace, patches)

            result = None
            for turn in case.turns:
                result = agent.chat(turn)
            trace.final_response = result

        except Exception as e:  # noqa: BLE001 —— 单条用例异常不应中断整轮评估
            trace.error = f"{type(e).__name__}: {e}"
        finally:
            for obj, attr, original in patches:
                setattr(obj, attr, original)
            if agent is not None:
                self._close_tool_managers(agent)
            # 注意：刻意不调用 agent.close()，避免长期记忆巩固写入

        return trace

    # ---------- 插桩 ----------
    def _instrument(self, agent, trace: RunTrace, patches: list[tuple]) -> None:
        """给共享 client、各 ToolManager、（多 Agent）Router 打补丁。"""
        # 1) LLM client：create + beta.parse
        completions = agent.client.chat.completions
        patches.append((completions, "create", completions.create))
        completions.create = self._wrap_create(completions.create, trace)

        beta_completions = agent.client.beta.chat.completions
        patches.append((beta_completions, "parse", beta_completions.parse))
        beta_completions.parse = self._wrap_parse(beta_completions.parse, trace)

        # 2) 工具执行
        for tm in self._tool_managers(agent):
            patches.append((tm, "execute_tool", tm.execute_tool))
            tm.execute_tool = self._wrap_execute_tool(tm.execute_tool, trace)

        # 3) 多 Agent 路由
        if self.mode == "multi" and hasattr(agent, "router"):
            patches.append((agent.router, "route", agent.router.route))
            agent.router.route = self._wrap_route(agent.router.route, trace)

    def _wrap_create(self, original, trace: RunTrace):
        def wrapper(*args, **kwargs):
            start = time.time()
            response = original(*args, **kwargs)
            latency_ms = (time.time() - start) * 1000
            self._record_llm_call(
                trace, response, latency_ms,
                purpose=self._guess_purpose(kwargs),
            )
            return response
        return wrapper

    def _wrap_parse(self, original, trace: RunTrace):
        def wrapper(*args, **kwargs):
            start = time.time()
            response = original(*args, **kwargs)
            latency_ms = (time.time() - start) * 1000
            self._record_llm_call(trace, response, latency_ms, purpose="extract")
            return response
        return wrapper

    def _wrap_execute_tool(self, original, trace: RunTrace):
        def wrapper(name: str, arguments: dict) -> str:
            result_str = original(name, arguments)
            trace.tool_observations.append(
                ToolObservation(name=name, arguments=dict(arguments), result=result_str)
            )
            return result_str
        return wrapper

    def _wrap_route(self, original, trace: RunTrace):
        def wrapper(*args, **kwargs):
            agent_key = original(*args, **kwargs)
            trace.route = agent_key
            return agent_key
        return wrapper

    # ---------- 辅助 ----------
    @staticmethod
    def _guess_purpose(kwargs: dict) -> str:
        """启发式标注 LLM 调用用途，仅供报告可读，不作硬断言。"""
        if kwargs.get("max_tokens") == 10:
            return "router"
        if kwargs.get("tools"):
            return "react"
        return "react"

    @staticmethod
    def _record_llm_call(trace: RunTrace, response, latency_ms: float, purpose: str) -> None:
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", 0) or 0

        tool_calls: list[dict] = []
        try:
            message = response.choices[0].message
            for tc in (getattr(message, "tool_calls", None) or []):
                tool_calls.append({
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                })
        except (AttributeError, IndexError):
            pass

        model = getattr(response, "model", "") or ""
        trace.llm_calls.append(LLMCallRecord(
            purpose=purpose,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            tool_calls=tool_calls,
            latency_ms=latency_ms,
        ))

    def _tool_managers(self, agent) -> list:
        # B1/H1.0 后总控暴露 profiles(画像含各自 tool_manager),不再有 .agents
        if self.mode == "multi" and hasattr(agent, "profiles"):
            return [p["tool_manager"] for p in agent.profiles.values()]
        if hasattr(agent, "tool_manager"):
            return [agent.tool_manager]
        return []

    def _close_tool_managers(self, agent) -> None:
        for tm in self._tool_managers(agent):
            try:
                tm.close()
            except Exception:  # noqa: BLE001
                pass
