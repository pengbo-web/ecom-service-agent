"""卖家编排器:切画像、事件流、teardown 复位与逐个关闭、接口与买家编排器一致。

用真实的 SellerOrchestrator 实例(不是 __new__ 跳过 __init__ 的空壳),但把
EcomAgent / ToolManager / SellerRouter 都换成打桩实现,只在这三个边界打桩——
`chat()`/`close()` 里对 engine 和 tool_manager 做的事情全部按真实路径跑一遍,
断言落在"可观察效果"上:engine 最终携带哪个画像的 prompt/tool_manager、
event_sink 收到了什么路由事件、close() 后 engine 的 tool_manager 是否复位、
每个画像的 tool_manager 是否都被关闭过。
"""

from unittest.mock import MagicMock

import pytest


class FakeToolManager:
    """替身 ToolManager:记录自己被分配的工具集合与是否被 close() 过。"""

    def __init__(self, use_mcp=None, mcp_server_url=None, allowed_tools=None):
        self.allowed_tools = allowed_tools
        self.closed = False

    def close(self):
        self.closed = True


class FakeEngine:
    """替身 EcomAgent:只暴露 SellerOrchestrator 会读写的那几个属性/方法。"""

    def __init__(self, *a, **kw):
        self.client = MagicMock(name="engine_client")
        self.model = "fake-model"
        self.raw_messages = []
        self.session_id = "sid-1"
        self.user_id = "uid-1"
        self._pending = None
        self.system_prompt = "<original prompt>"
        self.tool_manager = "ORIGINAL_TOOL_MANAGER"   # 引擎自带的全量工具(哨兵值)
        self.event_sink = None
        self.closed = False
        self.chat_calls: list[str] = []

    def chat(self, user_input: str):
        self.chat_calls.append(user_input)
        return f"reply:{user_input}"

    def close(self):
        self.closed = True

    def reset(self):
        pass

    def save(self):
        pass


FAKE_SELLER_PROFILES = {
    "analyst": {"name": "参谋-小策", "prompt": "ANALYST_PROMPT", "tools": {"shop_overview"}},
    "growth": {"name": "增长-小拓", "prompt": "GROWTH_PROMPT", "tools": {"draft_outreach"}},
}


class FakeRouter:
    """替身 SellerRouter:route() 返回值由测试固定,不真的调用 LLM。"""

    def __init__(self, client, model):
        self.client = client
        self.model = model
        self.forced_key = "analyst"

    def route(self, user_input, history=None):
        return self.forced_key


@pytest.fixture()
def orch(monkeypatch, tmp_path):
    """真实构造的 SellerOrchestrator,三处外部依赖(引擎/工具管理器/路由器)打桩。

    另外隔离数据库:SellerOrchestrator.chat() 会经 `_shared_context_block()` →
    `shared_context.recent_entries()` 读全局单例 get_db(),默认指向进程级的
    `app/sessions/ecom.db`——这张本地开发用的 scratch 库会跨次真实运行累积
    diagnosis 行。不隔离的话,下面对 engine.system_prompt 的断言就会随"这台
    机器之前有没有真的跑过 app"而变(报告里记录过一次真实的失败)。改成每个
    测试各自一份 tmp_path 下的临时 sqlite 文件,测试结束后把单例复位——
    与 tests/test_growth_api.py 的 client fixture 同一手法。
    """
    monkeypatch.setattr("app.agent.chat.EcomAgent", FakeEngine)
    monkeypatch.setattr("app.multi_agent.agents.SELLER_AGENT_CONFIGS", FAKE_SELLER_PROFILES)
    monkeypatch.setattr("app.multi_agent.orchestrator.ToolManager", FakeToolManager)
    monkeypatch.setattr("app.multi_agent.seller_router.SellerRouter", FakeRouter)

    # shared_context 默认已改为 redis 后端;测试用 SQLite,显式切回。
    from app.config import settings as _st
    monkeypatch.setattr(_st.settings, "shared_context_backend", "sqlite")

    from app.db import Database, set_db
    db = Database(db_path=str(tmp_path / "seller_orchestrator_test.db"))
    db.init_schema()
    set_db(db)

    from app.multi_agent.orchestrator import SellerOrchestrator
    o = SellerOrchestrator()
    yield o
    set_db(None)


def test_seller_orchestrator_exposes_same_surface():
    from app.multi_agent.orchestrator import MultiAgentOrchestrator, SellerOrchestrator
    for name in ("chat", "save", "close", "reset", "raw_messages", "session_id"):
        assert hasattr(SellerOrchestrator, name), name
        assert hasattr(MultiAgentOrchestrator, name), name


def test_buyer_orchestrator_profiles_unchanged():
    """买家链路零改动的机械保证:画像键必须仍是这三个。"""
    from app.multi_agent.agents import AGENT_CONFIGS
    assert set(AGENT_CONFIGS) == {"presale", "midsale", "aftersale"}


def test_chat_routes_to_analyst_installs_its_profile_on_engine(orch):
    """路由到 analyst:engine 最终携带 analyst 的 prompt + 专属 tool_manager。

    顺带用测试自己种的一条共享上下文(不借别的会话/别的测试真的留在磁盘上的
    行),证明"注入到 prompt"这件事确实发生。断言 prompt 以画像正文开头而不是
    整串相等——这样不论 shared_context 里有没有行,这条断言都成立,不再靠
    "本机之前有没有真的跑过 app"这种运气。
    """
    from app.multi_agent import shared_context as sc
    sc.share(sc.KEY_DIAGNOSIS, "P001", {"conclusion": "退款率偏高"}, "analyst", "C-test")

    orch.router.forced_key = "analyst"
    reply = orch.chat("这周退款率怎么样")

    assert reply == "reply:这周退款率怎么样"
    assert orch.engine.system_prompt.startswith("ANALYST_PROMPT")
    assert "【共享上下文开始】" in orch.engine.system_prompt   # 自己种的那条确实注入了
    assert orch.engine.tool_manager is orch.profiles["analyst"]["tool_manager"]
    assert orch.engine.tool_manager.allowed_tools == {"shop_overview"}
    assert orch.last_agent_key == "analyst"


def test_chat_routes_to_growth_installs_its_profile_on_engine(orch):
    """路由到 growth:engine 换成 growth 的 prompt + 专属(写能力)tool_manager。

    这条刻意不种共享上下文,覆盖"没有行"这条路径。同样断言 prompt 以画像正文
    开头而非整串相等——不依赖 shared_context 是否为空。
    """
    orch.router.forced_key = "growth"
    reply = orch.chat("帮我给沉默下单的人写条触达文案")

    assert reply == "reply:帮我给沉默下单的人写条触达文案"
    assert orch.engine.system_prompt.startswith("GROWTH_PROMPT")
    assert orch.engine.tool_manager is orch.profiles["growth"]["tool_manager"]
    assert orch.engine.tool_manager.allowed_tools == {"draft_outreach"}
    assert orch.last_agent_key == "growth"


def test_chat_emits_routing_event_naming_the_chosen_persona(orch):
    """event_sink 必须收到一条 route 事件,agent/key 与实际路由结果一致。"""
    events = []
    orch.event_sink = events.append

    orch.router.forced_key = "growth"
    orch.chat("有哪些客户可以触达")

    assert len(events) == 1
    assert events[0]["type"] == "route"
    assert events[0]["key"] == "growth"
    assert events[0]["agent"] == "增长-小拓"
    assert events[0]["actor"] == "seller"


def test_close_restores_original_tool_manager_and_closes_every_profile(orch):
    """close() 后:engine.tool_manager 复位为引擎原生的;每个画像的 tool_manager 都被关闭。"""
    orch.chat("这周退款率怎么样")   # 先切成 analyst 画像,证明 close 真的把它换回去了
    assert orch.engine.tool_manager is not orch._default_tm

    orch.close()

    assert orch.engine.tool_manager is orch._default_tm
    assert orch.engine.tool_manager == "ORIGINAL_TOOL_MANAGER"
    assert orch.engine.closed is True
    for p in orch.profiles.values():
        assert p["tool_manager"].closed is True
