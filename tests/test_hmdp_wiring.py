"""C1/I1 回归:hmdp token 从 /api/chat → 流式 worker → 工具上下文;client 优雅降级。"""

from app.api.streaming import run_agent_streaming
from app.agent.runtime_context import get_current_token
from app.schemas.response import CustomerServiceResponse, IntentType
from mcp_server.hmdp_client import HmdpClient


class _FakeAgent:
    def __init__(self):
        self.raw_messages = []
        self.user_id = "5"
        self._turn_qu = None
        self.captured_token = None

    def set_turn_understanding(self, q):
        pass

    def chat(self, msg):
        # 工具在此上下文里执行 —— 记录当前线程能否拿到 hmdp token
        self.captured_token = get_current_token()
        return CustomerServiceResponse(intent=IntentType.OTHER, confidence=0.9,
                                       reply="ok", requires_human=False, follow_up_question=None)


def test_streaming_sets_current_token_in_worker():
    """C1:run_agent_streaming 收到的 hmdp_token 必须落到 worker 线程的 current_token,
    否则 MCP 调 hmdp 登录接口无凭据(订单类工具全 401)。"""
    agent = _FakeAgent()
    list(run_agent_streaming(agent, "你好", session_id="s1", hmdp_token="tok-xyz"))
    assert agent.captured_token == "tok-xyz"


def test_streaming_no_token_is_none():
    agent = _FakeAgent()
    list(run_agent_streaming(agent, "你好", session_id="s1"))
    assert agent.captured_token is None


class _Resp:
    def __init__(self, status, payload=None, raise_json=False):
        self.status_code = status
        self._payload = payload
        self._raise = raise_json

    def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._payload


def test_client_safe_401_degrades():
    assert HmdpClient._safe(_Resp(401)) == {"success": False, "errorMsg": "未登录或登录已过期"}


def test_client_safe_non_json_degrades():
    r = HmdpClient._safe(_Resp(500, raise_json=True))
    assert r["success"] is False and "异常" in r["errorMsg"]


def test_client_safe_ok_passthrough():
    assert HmdpClient._safe(_Resp(200, {"success": True, "data": 1})) == {"success": True, "data": 1}
