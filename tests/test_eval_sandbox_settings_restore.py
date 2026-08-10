"""回归:评估沙箱必须还原它改过的**进程级** settings。

为什么这条值得单独一个文件钉住:`Sandbox._build_agent` 关记忆/关 MCP 用的是
对全局单例 `settings` 的直接赋值(EcomAgent.__init__ 只读全局,没有构造参数
可传),曾经写完从不还原。后果不止是测试互相污染——`POST /api/eval/run`
(看板"运行评估"按钮)是在**服务进程内**起线程跑评估的(app/evaluation/runner.py
EvalRunner.start),所以线上点一次评估就会把整个进程的记忆与 MCP 永久关掉
直到重启,而且悄无声息。

这个缺陷当时没被任何测试发现,因为它的症状出现在**别的**测试/别的请求里。
"""

from __future__ import annotations

import pytest

from app.config.settings import settings
from app.evaluation.dataset import EvalCase
from app.evaluation.sandbox import Sandbox


@pytest.fixture
def _restore_settings():
    """兜底:即便被测代码没还原,也不让这个文件自己变成新的污染源。"""
    saved = {k: getattr(settings, k) for k in Sandbox._ISOLATED_SETTINGS}
    yield
    for k, v in saved.items():
        setattr(settings, k, v)


def test_build_agent_registers_settings_for_restore(tmp_path, _restore_settings):
    """_build_agent 改全局 settings 时,必须把原值登记进 patches 供还原。"""
    settings.memory_enabled = True
    settings.mcp_enabled = True

    sandbox = Sandbox(mode="single", tmp_root=str(tmp_path))
    patches: list = []
    agent = sandbox._build_agent(sandbox.session_path_for("c1"), patches)
    try:
        # 隔离确实生效(沙箱期间关掉,否则记忆会读 default.json 污染评分)
        assert settings.memory_enabled is False
        assert settings.mcp_enabled is False
        # 且原值已登记,可还原
        restored = {attr: original for obj, attr, original in patches if obj is settings}
        assert restored == {"memory_enabled": True, "mcp_enabled": True}
    finally:
        sandbox._close_tool_managers(agent)
        for obj, attr, original in patches:
            setattr(obj, attr, original)

    assert settings.memory_enabled is True and settings.mcp_enabled is True


def test_run_restores_settings_even_when_case_fails(tmp_path, monkeypatch,
                                                    _restore_settings):
    """run() 的 finally 必须还原——**用例跑失败时也要还原**。

    失败路径才是关键:线上那次评估如果中途抛错,更没有理由让服务进程带着
    "记忆已关闭"继续服务买家。这里用插桩阶段抛错来触发失败路径,顺带避免
    真的去调模型(本套件离线跑)。
    """
    settings.memory_enabled = True
    settings.mcp_enabled = True

    def _boom(*a, **k):
        raise RuntimeError("instrument failed")

    monkeypatch.setattr(Sandbox, "_instrument", _boom)

    sandbox = Sandbox(mode="single", tmp_root=str(tmp_path))
    trace = sandbox.run(EvalCase(id="c1", description="还原回归用", turns=["你好"]))

    assert trace.error and "instrument failed" in trace.error   # 确实走了失败路径
    assert settings.memory_enabled is True                       # 仍被还原
    assert settings.mcp_enabled is True
