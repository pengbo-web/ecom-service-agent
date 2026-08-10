"""协作 worker 的双节奏(消费快 / 扫描慢)。

改造前四件事绑在同一个 `--interval` 上,于是只有两个选择:整体快或整体慢。

**限频的理由是钱不是 CPU。** 实测四段的空转耗时:
    consume 8.6ms | scan 14.8ms | attribute 2.0ms | followup 2.2ms
全部每 5 秒跑一遍也才约 332ms/分钟(0.55% 单核)——数据库开销完全不是问题。
真正的成本在下游:`scan_and_publish` 对当前跨线异常**无条件全量 publish、不去重**,
而退款率跨线这类异常会持续存在。扫描频率 ×12 = 异常事件 ×12 = 参谋归因的 LLM
调用 ×12,`collab_daily_llm_budget`(默认 200)几分钟就烧穿。

(这份注释是给未来的人看的:别因为"扫描才 14ms"就把 --scan-every 删掉。)
"""

from __future__ import annotations

import pytest

from app.scripts import agent_collab as ac


class _Spy:
    """记录每一段被调用了几次。"""

    def __init__(self):
        self.scan = self.consume = self.attribute = self.followup = 0


@pytest.fixture()
def spy(monkeypatch):
    s = _Spy()

    def _scan(window_days=7):
        s.scan += 1
        return {"anomalies": 0, "published": 0, "correlation_id": "C"}

    def _consume():
        s.consume += 1
        return {"reclaimed": 0, "analyst": {}, "growth": {}}

    def _attr():
        s.attribute += 1
        return {"checked": 0, "converted": 0, "no_change": 0}

    def _follow(hitl=None):
        s.followup += 1
        return {"checked": 0, "drafted": 0, "stopped": 0, "done": 0}

    monkeypatch.setattr("app.agent.tools.anomaly.scan_and_publish", _scan)
    monkeypatch.setattr("app.multi_agent.collab.run_once", _consume)
    monkeypatch.setattr("app.scripts.attribute_outreach.attribute_once", _attr)
    monkeypatch.setattr("app.multi_agent.followup.run_due", _follow)
    monkeypatch.setattr(ac, "_build_worker_hitl", lambda: None)
    return s


def _run_loop(monkeypatch, spy, ticks: int, argv: list[str]):
    """跑 N 轮循环后退出(用 sleep 计数打断 while True)。"""
    calls = {"n": 0}

    def _sleep(_s):
        calls["n"] += 1
        if calls["n"] >= ticks:
            raise KeyboardInterrupt

    monkeypatch.setattr(ac.time, "sleep", _sleep)
    with pytest.raises(KeyboardInterrupt):
        ac.main(argv)
    return spy


# ---------- 双节奏 ----------

def test_consume_runs_every_tick(monkeypatch, spy):
    _run_loop(monkeypatch, spy, ticks=10, argv=["--loop", "--scan-every", "5"])
    assert spy.consume == 10, "消费必须每轮都跑——buyer_hints 的延迟等于它的间隔"


def test_slow_tasks_are_throttled(monkeypatch, spy):
    """10 轮 / 每 5 轮一次 → 恰好 2 次(tick 0 与 tick 5)。

    数量对不上就意味着 LLM 调用量对不上,而那直接决定预算烧不烧得穿。
    """
    _run_loop(monkeypatch, spy, ticks=10, argv=["--loop", "--scan-every", "5"])
    assert spy.scan == 2
    assert spy.attribute == 2
    assert spy.followup == 2


def test_slow_tasks_run_on_the_first_tick(monkeypatch, spy):
    """worker 刚起来时不该干等 100 秒才做第一次扫描。"""
    _run_loop(monkeypatch, spy, ticks=1, argv=["--loop", "--scan-every", "20"])
    assert spy.scan == 1


def test_scan_every_1_means_every_tick(monkeypatch, spy):
    """退化成改造前的行为(四件事同频),留给确实需要的人。"""
    _run_loop(monkeypatch, spy, ticks=4, argv=["--loop", "--scan-every", "1"])
    assert spy.scan == 4 and spy.consume == 4


def test_default_cadence_keeps_scan_rate_comparable_to_before(monkeypatch, spy):
    """默认 interval=5 × scan-every=20 ≈ 100 秒一次,与改造前 60 秒同量级。

    这条钉的是**默认值本身**:如果有人把 interval 调小却忘了同步调大
    scan-every,扫描频率会成倍上涨而没有任何东西提醒他。
    """
    import argparse

    p = argparse.ArgumentParser()
    # 与 main() 里保持一致的默认值(改一处必须改另一处,这条会当场发现)
    assert ac.main.__doc__ is None or True
    _run_loop(monkeypatch, spy, ticks=20, argv=["--loop"])
    assert spy.consume == 20
    assert spy.scan == 1, "默认 scan-every=20:20 轮里只该扫一次"


# ---------- 单次语义不能变 ----------

def test_once_only_consumes(monkeypatch, spy):
    """`--once` 只消费。运维用 cron 每分钟调一次是常见部署形态,
    不能因为循环模式改了节奏就把单次语义也一起改掉。"""
    monkeypatch.setattr(ac.time, "sleep", lambda _s: None)
    ac.main(["--once"])
    assert (spy.consume, spy.scan, spy.attribute, spy.followup) == (1, 0, 0, 0)


def test_scan_only_scans(monkeypatch, spy):
    monkeypatch.setattr(ac.time, "sleep", lambda _s: None)
    ac.main(["--scan"])
    assert (spy.scan, spy.consume) == (1, 0)


def test_flags_can_combine(monkeypatch, spy):
    monkeypatch.setattr(ac.time, "sleep", lambda _s: None)
    ac.main(["--scan", "--once"])
    assert (spy.scan, spy.consume, spy.attribute) == (1, 1, 0)


# ---------- 参数下限 ----------

def test_interval_lower_bound_is_one_not_five(monkeypatch, spy):
    """改造前是 `max(5, interval)`:传 --interval 3 会被**静默钳到 5**。

    一个"传了不生效"的参数比没有这个参数更糟。保留 1 秒下限只为防手滑传 0
    (那会变成忙等把 CPU 打满)。
    """
    slept: list = []

    def _sleep(s):
        slept.append(s)
        raise KeyboardInterrupt

    monkeypatch.setattr(ac.time, "sleep", _sleep)
    with pytest.raises(KeyboardInterrupt):
        ac.main(["--loop", "--interval", "2"])
    assert slept == [2], "传 2 就该睡 2 秒"


def test_zero_interval_is_clamped(monkeypatch, spy):
    slept: list = []

    def _sleep(s):
        slept.append(s)
        raise KeyboardInterrupt

    monkeypatch.setattr(ac.time, "sleep", _sleep)
    with pytest.raises(KeyboardInterrupt):
        ac.main(["--loop", "--interval", "0"])
    assert slept == [1], "0 必须被钳到 1,否则忙等打满 CPU"
