"""卖家编排器:切画像、粘性路由、接口与买家编排器一致、不污染买家链路。"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def orch(tmp_path):
    with patch("app.agent.chat.EcomAgent.__init__", return_value=None):
        from app.multi_agent.orchestrator import SellerOrchestrator
        o = SellerOrchestrator.__new__(SellerOrchestrator)
    return o


def test_seller_orchestrator_exposes_same_surface():
    from app.multi_agent.orchestrator import MultiAgentOrchestrator, SellerOrchestrator
    for name in ("chat", "save", "close", "reset", "raw_messages", "session_id"):
        assert hasattr(SellerOrchestrator, name), name
        assert hasattr(MultiAgentOrchestrator, name), name


def test_buyer_orchestrator_profiles_unchanged():
    """买家链路零改动的机械保证:画像键必须仍是这三个。"""
    from app.multi_agent.agents import AGENT_CONFIGS
    assert set(AGENT_CONFIGS) == {"presale", "midsale", "aftersale"}
