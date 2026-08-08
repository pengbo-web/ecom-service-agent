"""W1 服务化 L2:create_app() 接入预热的集成验证——

- 预热只在生产路径(未注入 session_manager)启动,与既有的空闲回收线程同一个判断,
  不会污染测试(注入 session_manager 时保持零后台线程,这是既有约定)。
- 预热绝不阻塞服务就绪:即使某一步很慢,/api/health 必须立刻可用。
- 预热全部失败时,服务仍然完全可用(健康检查 + 会话构造都正常)。
- 开关关闭时不起预热线程。
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api import app as app_module
from app.api import warmup as warmup_module
from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.config.settings import settings


@pytest.fixture(autouse=True)
def _restore_warmup_flag():
    orig = settings.startup_warmup_enabled
    yield
    settings.startup_warmup_enabled = orig


def test_injecting_session_manager_skips_warmup_thread():
    """与既有 reaper 的约定一致:测试注入 session_manager 时不应额外起后台线程
    (否则每个用 bare 参数跑测试套件的地方都会多一条真实预热线程)。"""
    mgr = SessionManager(agent_factory=lambda p, u=None: object())
    app = create_app(session_manager=mgr)
    assert app.state.warmup_thread is None


def test_startup_warmup_disabled_flag_skips_thread():
    settings.startup_warmup_enabled = False
    app = create_app()
    assert app.state.warmup_thread is None


def test_warmup_thread_starts_and_does_not_block_readiness(monkeypatch):
    """核心约束①:预热必须在后台跑,create_app() 不能等它。用一个明显很慢
    (5s)的预热步骤验证:create_app() 会在这一步跑完**之前**就返回,且
    /api/health 在预热仍在后台运行期间就已经可用——不会因为预热还没跑完
    而卡住第一个请求。(不断言 create_app() 本身的绝对耗时——它还有一堆
    与本任务无关的既有初始化开销,如 demo 模式下尝试连 Redis 写身份等，
    那部分耗时不是这里要验证的东西;这里只验证"没有被预热多拖住"。)"""
    started = threading.Event()
    finished = threading.Event()

    def slow_step():
        started.set()
        time.sleep(5.0)
        finished.set()

    monkeypatch.setattr(warmup_module, "_STEPS", [("slow", slow_step)])

    app = create_app()

    # create_app() 已经返回,而 5s 的预热步骤显然还没跑完——这才是"不阻塞
    # 服务就绪"真正要证明的东西,与 create_app() 自身基线耗时无关。
    assert not finished.is_set(), "create_app() 不应等预热跑完才返回"
    assert app.state.warmup_thread is not None
    assert isinstance(app.state.warmup_thread, threading.Thread)
    assert app.state.warmup_thread.is_alive()

    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert not finished.is_set(), "健康检查响应时,慢预热步骤仍应在后台运行"

    assert started.wait(timeout=2), "后台预热线程应确实在跑"
    app.state.warmup_thread.join(timeout=8)   # 收尾,避免测试进程遗留计时器
    assert finished.is_set()


def test_service_stays_functional_when_every_warmup_step_fails(monkeypatch):
    """核心约束②:预热失败绝不能让服务变得不可用——健康检查与真实的会话
    构造路径都必须照常工作,首个请求只是照付它今天已经在付的成本。"""
    def boom():
        raise RuntimeError("warmup deliberately broken for this test")

    monkeypatch.setattr(warmup_module, "_STEPS", [
        ("mcp", boom), ("llm_transport", boom), ("faq_cache", boom), ("kb_retriever", boom),
    ])

    app = create_app()
    app.state.warmup_thread.join(timeout=5)   # 等预热真正跑完(全部失败)再验证服务

    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200

    # 会话构造(get_or_create)本身与预热是否成功无关——用真实的 app.state.session_manager
    # 验证服务的核心能力(拿/建会话)没有被预热失败连累。
    manager = app.state.session_manager
    from app.multi_agent.orchestrator import MultiAgentOrchestrator
    import app.api.session_manager as sm_mod
    agent = manager.get_or_create("warmup-failure-smoke-session", "u1")
    assert isinstance(agent, MultiAgentOrchestrator)


def test_warmup_thread_reports_success_signal(monkeypatch, capsys):
    """耗时/成败信号必须真的发出来(可观测性铁律)——这里验证的是集成路径
    (通过 create_app() 走到的真实 warm_process()),不是单独调用 warm_process()。"""
    monkeypatch.setattr(warmup_module, "_STEPS", [("noop", lambda: None)])
    app = create_app()
    app.state.warmup_thread.join(timeout=5)

    out = capsys.readouterr().out
    assert "启动预热" in out
    assert "全部成功" in out
