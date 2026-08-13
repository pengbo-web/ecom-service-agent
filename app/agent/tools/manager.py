"""ToolManager：统一管理本地工具和 MCP 工具。

当 MCP 启用时，通过 Streamable HTTP 连接 MCP Server 获取工具；
当 MCP 未启用或连接失败时，退回本地工具。
"""

import json
from typing import Optional

from app.agent.tools.registry import TOOL_DEFINITIONS as LOCAL_TOOL_DEFINITIONS
from app.agent.tools.registry import execute_tool as local_execute_tool


class ToolManager:
    """聚合本地工具和 MCP 工具，提供统一的工具定义和调度接口。"""

    def __init__(
        self,
        use_mcp: bool = False,
        mcp_server_url: str = "",
        allowed_tools: Optional[set] = None,
    ):
        self._mcp_client = None
        self._shared_mcp = False
        self._tool_source: dict[str, str] = {}
        self._tool_defs: list[dict] = []

        if use_mcp and mcp_server_url:
            self._init_mcp(mcp_server_url)
        else:
            self._init_local()

        if allowed_tools is not None:
            self._filter_tools(allowed_tools)

    def _init_local(self):
        """只加载本地工具。"""
        self._tool_defs = list(LOCAL_TOOL_DEFINITIONS)
        for td in self._tool_defs:
            self._tool_source[td["function"]["name"]] = "local"

    def _init_mcp(self, server_url: str):
        """从进程级共享连接取 MCP 工具;失败降级本地。"""
        from app.mcp_client import get_shared_mcp_client

        shared = get_shared_mcp_client(server_url)
        if shared is None:
            print(f"⚠️  [MCP] 连接失败,降级使用本地工具")
            self._init_local()
            return

        self._mcp_client, mcp_tools = shared
        self._shared_mcp = True

        mcp_names = set()
        for td in list(mcp_tools):            # 拷贝,不改共享列表
            name = td["function"]["name"]
            mcp_names.add(name)
            self._tool_source[name] = "mcp"
        self._tool_defs = list(mcp_tools)

        for td in LOCAL_TOOL_DEFINITIONS:
            name = td["function"]["name"]
            if name not in mcp_names:
                self._tool_defs.append(td)
                self._tool_source[name] = "local"

    def _filter_tools(self, allowed: set):
        """只保留白名单中的工具，用于子 Agent 工具隔离。"""
        self._tool_defs = [
            d for d in self._tool_defs
            if d["function"]["name"] in allowed
        ]
        self._tool_source = {
            k: v for k, v in self._tool_source.items()
            if k in allowed
        }

    @property
    def tool_definitions(self) -> list[dict]:
        return self._tool_defs

    @property
    def tool_names(self) -> list[str]:
        """本轮**实际装上**的工具名(已过 allowed 过滤)。

        供出话泄漏检测(`app/agent/jargon_guard.py`)当词表用。与 `SkillManager.skill_names`
        同一条纪律:检测器不自己维护一份工具清单,从这个真源取——手抄表跟真源 drift
        是这个仓库反复出过的问题。

        取过滤后的 `_tool_defs` 而不是全量注册表:没装上的工具模型不可能说出来,
        把它们放进词表只会增加误报面。
        """
        return [d["function"]["name"] for d in self._tool_defs
                if d.get("function", {}).get("name")]

    def execute_tool(self, name: str, arguments: dict) -> str:
        """分发调用;结果超长则落盘留指针(不丢信息),防单条结果撑爆上下文窗口。"""
        source = self._tool_source.get(name)

        if source == "mcp" and self._mcp_client:
            # 跨进程身份透传:MCP 工具在独立 server 进程执行,ContextVar 传不过去,
            # 用保留参数把当前用户带过去(server 端 set_current_user 后 owned_order 才能校验)。
            #
            # **确认门(consent)同样传不过去,而它比身份更容易被漏掉。** 实测:
            # 客户端已 `consent_scope(['refund'])`,MCP 路径上 `apply_refund` 仍然
            # 返回 need_confirm——买家确认→工具再问一次→无限循环,**退款永远完不成**。
            # 方向是安全的(不会越权执行),但功能是坏的,而 mcp_enabled=True 就是
            # 当前部署配置。
            #
            # 注意保留参数放在 `**arguments` **之后**:模型自己在 arguments 里塞
            # ctx_user_id/ctx_consent 也会被这里的真值覆盖,自授权无效。
            from app.agent.consent import allowed_actions
            from app.agent.runtime_context import get_current_user, get_current_token
            args = {**arguments, "ctx_user_id": get_current_user() or "",
                    "ctx_token": get_current_token() or "",
                    "ctx_consent": ",".join(sorted(allowed_actions()))}
            result = self._mcp_client.call_tool(name, args)
        elif source == "local":
            result = local_execute_tool(name, arguments)
        else:
            result = json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

        return self._maybe_offload(name, result)

    # 读取工具本身豁免(否则读回内容又被落盘,形成死循环)
    _OFFLOAD_EXEMPT = {"read_tool_result"}

    def _maybe_offload(self, name: str, result: str) -> str:
        from app.config.settings import settings
        if name in self._OFFLOAD_EXEMPT or not isinstance(result, str):
            return result
        limit = settings.tool_result_max_chars
        if len(result) <= limit:
            return result
        from app.agent.tools.result_store import get_result_store
        ref = get_result_store().save(result)
        return json.dumps({
            "truncated": True,
            "result_ref": ref,
            "total_chars": len(result),
            "preview": result[:settings.tool_result_preview_chars],
            "note": (f"结果过长已存档。若预览不足以回答,请调用 "
                     f"read_tool_result(ref='{ref}', offset=0, length=4000) 分段读取完整内容。"),
        }, ensure_ascii=False)

    def close(self):
        """清理:仅关闭本 ToolManager 独占的 MCP 连接;共享连接由 reset_shared_mcp/进程管。"""
        if self._mcp_client and not self._shared_mcp:
            self._mcp_client.close()
        self._mcp_client = None
