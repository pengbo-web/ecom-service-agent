"""记忆更新节流:STM 每 N 轮 + 异步;中途抽取游标。全离线(fake client)。"""

import threading

from app.agent.memory.manager import MemoryManager
from app.config.settings import settings


class FakeClient:
    """脚本化 chat.completions.create;记录调用并返回固定事实(逐行格式,匹配
    extraction.py 现行的按行解析,非 JSON.loads——见 test_memory_throttle 报告说明)。"""

    def __init__(self, reply="用户偏好红色"):
        self.calls = []
        self.reply = reply
        outer = self

        class _C:
            def create(self, **kw):
                outer.calls.append(kw)
                class _Msg:  # noqa: N801
                    content = outer.reply
                class _Choice:
                    message = _Msg()
                class _Resp:
                    choices = [_Choice()]
                    usage = None
                return _Resp()
        class _Chat:
            completions = _C()
        self.chat = _Chat()


def _mgr(tmp_path, **kw):
    kw.setdefault("memory_enabled", True)
    return MemoryManager(client=FakeClient(), model="m", user_id="u1",
                         memory_dir=str(tmp_path / "mem"), **kw)


MSGS = [{"role": "user", "content": "我喜欢红色"}]


def test_stm_updates_only_every_n_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 3)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    m = _mgr(tmp_path)
    for _ in range(6):
        m.update_short_term(MSGS, all_messages=MSGS)
    assert len(m.client.calls) == 2          # 第 3、6 轮各 1 次


def test_n_equals_1_keeps_legacy_every_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 1)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    m = _mgr(tmp_path)
    for _ in range(3):
        m.update_short_term(MSGS, all_messages=MSGS)
    assert len(m.client.calls) == 3


def test_disabled_memory_short_circuits_without_counting(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 3)
    m = _mgr(tmp_path, memory_enabled=False)
    for _ in range(6):
        m.update_short_term(MSGS, all_messages=MSGS)
    assert m.client.calls == [] and m._turn_count == 0


def test_async_mode_runs_in_background_and_updates_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 1)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    monkeypatch.setattr(settings, "memory_async_updates", True)
    m = _mgr(tmp_path)
    m.update_short_term(MSGS, all_messages=MSGS)
    assert m._bg_thread is not None
    m._bg_thread.join(timeout=5)
    assert m.stm.facts == ["用户偏好红色"]
    # 后台线程不是主线程
    assert m._bg_thread is not threading.current_thread()


def test_async_snapshot_isolated_from_caller_mutation(tmp_path, monkeypatch):
    """传入列表在派发后被主线程继续 append,后台看到的是快照。"""
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 1)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    monkeypatch.setattr(settings, "memory_async_updates", True)
    m = _mgr(tmp_path)
    msgs = [{"role": "user", "content": "A"}]
    m.update_short_term(msgs, all_messages=msgs)
    msgs.append({"role": "user", "content": "B"})     # 派发后突变
    m._bg_thread.join(timeout=5)
    sent = m.client.calls[0]
    assert "B" not in str(sent)                        # 快照不含突变


def test_bg_exception_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 1)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    monkeypatch.setattr(settings, "memory_async_updates", True)
    m = _mgr(tmp_path)
    def boom(*a, **k):
        raise RuntimeError("boom")
    m.stm.update = boom
    m.update_short_term(MSGS, all_messages=MSGS)       # 不抛
    m._bg_thread.join(timeout=5)                        # 线程正常结束
