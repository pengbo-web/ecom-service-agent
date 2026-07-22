"""Multi-Agent 协作模块。

- Router：意图分类,按用户意图路由到领域画像
- AGENT_CONFIGS：领域画像(专属 prompt + 工具子集)
- MultiAgentOrchestrator：编排器,路由到画像 + 用同一硬化引擎(EcomAgent)执行
"""

from app.multi_agent.agents import AGENT_CONFIGS
from app.multi_agent.orchestrator import MultiAgentOrchestrator
from app.multi_agent.router import Router

__all__ = ["MultiAgentOrchestrator", "Router", "AGENT_CONFIGS"]
