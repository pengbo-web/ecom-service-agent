"""W1 服务化 L2:启动预热——验证"后台跑/不阻塞/失败不致命/耗时与成败必须
可观测"这四条铁律，以及各预热步骤本身的边界(不碰外部 ApeRAG、开关关闭时不做多余工作)。
"""

import threading
import time

import pytest

from app.api import warmup


def test_warm_process_runs_all_steps_and_reports_success(monkeypatch):
    calls = []
    monkeypatch.setattr(warmup, "_STEPS", [
        ("a", lambda: calls.append("a")),
        ("b", lambda: calls.append("b")),
    ])
    result = warmup.warm_process()
    assert calls == ["a", "b"]
    assert result["ok"] is True
    assert set(result["steps"]) == {"a", "b"}
    assert result["steps"]["a"]["ok"] is True
    assert isinstance(result["steps"]["a"]["seconds"], float)
    assert isinstance(result["total_seconds"], float)


def test_step_failure_does_not_stop_other_steps(monkeypatch):
    calls = []

    def boom():
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(warmup, "_STEPS", [
        ("failing", boom),
        ("later", lambda: calls.append("later")),
    ])
    result = warmup.warm_process()
    assert calls == ["later"]                     # 后面的步骤照跑
    assert result["ok"] is False
    assert result["steps"]["failing"]["ok"] is False
    assert "simulated failure" in result["steps"]["failing"]["error"]
    assert result["steps"]["later"]["ok"] is True


def test_warm_process_never_raises(monkeypatch):
    """预热是优化,不是前置条件:任何步骤失败都不能让调用方看到异常。"""
    monkeypatch.setattr(warmup, "_STEPS", [
        ("boom", lambda: (_ for _ in ()).throw(RuntimeError("x"))),
    ])
    result = warmup.warm_process()   # 不应抛出
    assert result["ok"] is False


def test_warm_process_emits_observability_signal(monkeypatch, capsys):
    monkeypatch.setattr(warmup, "_STEPS", [("noop", lambda: None)])
    received = []
    result = warmup.warm_process(emit=lambda e: received.append(e))

    assert len(received) == 1
    assert received[0]["type"] == "startup_warmup"
    assert received[0]["ok"] is True
    assert "total_seconds" in received[0]

    out = capsys.readouterr().out
    assert "启动预热" in out    # 无论有没有接可观测性系统,控制台必须能看到


def test_emit_failure_does_not_break_warmup(monkeypatch):
    monkeypatch.setattr(warmup, "_STEPS", [("noop", lambda: None)])

    def bad_emit(_event):
        raise RuntimeError("observer exploded")

    result = warmup.warm_process(emit=bad_emit)   # 不应抛出
    assert result["ok"] is True


def test_background_warmup_does_not_block_caller(monkeypatch):
    """跑在后台线程,调用方必须立即拿回控制权——即使某一步很慢。"""
    started = threading.Event()
    finished = threading.Event()

    def slow_step():
        started.set()
        time.sleep(0.5)
        finished.set()

    monkeypatch.setattr(warmup, "_STEPS", [("slow", slow_step)])

    t0 = time.time()
    thread = warmup.warm_process_in_background()
    elapsed = time.time() - t0

    assert elapsed < 0.3, "warm_process_in_background() 不应等预热跑完才返回"
    assert thread.daemon is True
    assert started.wait(timeout=2), "后台线程应该确实在跑"
    assert thread.is_alive() or finished.is_set()
    thread.join(timeout=3)
    assert finished.is_set()


def test_mcp_warm_noop_when_disabled(monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "mcp_enabled", False)

    called = []
    monkeypatch.setattr(
        "app.mcp_client.get_shared_mcp_client",
        lambda url: called.append(url) or (object(), []),
    )
    warmup._warm_mcp()
    assert called == []   # 关掉时压根不该去连


def test_mcp_warm_raises_when_shared_client_unavailable(monkeypatch):
    """get_shared_mcp_client 本身是 fail-soft(失败返回 None);预热这一层要把
    它转成显式失败,好让 warm_process() 如实统计这一步没成功，而不是被
    误判成"什么都没干的成功"。"""
    from app.config.settings import settings
    monkeypatch.setattr(settings, "mcp_enabled", True)
    monkeypatch.setattr("app.mcp_client.get_shared_mcp_client", lambda url: None)

    with pytest.raises(RuntimeError):
        warmup._warm_mcp()


def test_kb_retriever_warm_never_touches_external_aperag(monkeypatch):
    """kb_backend=aperag 且 ApeRAG 当前不可达时,预热本地索引这一步绝不能
    去碰 aperag_search——碰了就是把"外部服务不可达"变成"预热挂起/失败"，
    正是任务里明确禁止的那类问题。"""
    from app.config.settings import settings
    monkeypatch.setattr(settings, "kb_backend", "aperag")

    def boom(*a, **kw):
        raise AssertionError("warm_local_retriever 不应触达外部 ApeRAG")

    monkeypatch.setattr("app.agent.recall.external_kb.aperag_search", boom)

    # 本地索引可能压根没建过(FileNotFoundError)——这是预热框架允许的
    # "这一步没什么可暖"结果，不是本测试要断言的重点；重点是不抛
    # AssertionError(即没有调用 aperag_search)。
    try:
        warmup._warm_kb_retriever()
    except FileNotFoundError:
        pass


def test_faq_cache_warm_is_idempotent_and_local_only():
    """FAQ 缓存预热只读本地文件,重复调用应该拿到同一个单例(不会每次预热
    都重新解析一遍文件)。"""
    from app.agent.faq_cache import get_faq_cache, reset_faq_cache
    reset_faq_cache()
    warmup._warm_faq_cache()
    first = get_faq_cache()
    warmup._warm_faq_cache()
    second = get_faq_cache()
    assert first is second
