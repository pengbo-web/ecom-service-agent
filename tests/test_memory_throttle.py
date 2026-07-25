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


def test_checkpoint_extracts_at_every_m_turns_with_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 100)   # 静音 STM,只看抽取
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 2)
    m = _mgr(tmp_path)
    all_msgs = []
    for i in range(4):
        all_msgs.append({"role": "user", "content": f"第{i}句"})
        m.update_short_term(MSGS, all_messages=all_msgs)
    # 第 2、4 轮各触发一次抽取
    assert len(m.client.calls) == 2
    # 第二次抽取只喂游标之后的新消息(不含"第0句")
    assert "第0句" not in str(m.client.calls[1])
    assert "第2句" in str(m.client.calls[1]) or "第3句" in str(m.client.calls[1])
    assert m._extract_cursor == len(all_msgs)


def test_checkpoint_disabled_when_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 100)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    m = _mgr(tmp_path)
    for i in range(4):
        m.update_short_term(MSGS, all_messages=[{"role": "user", "content": "x"}] * (i + 1))
    assert m.client.calls == []


def test_final_consolidate_only_tail_after_checkpoint(tmp_path, monkeypatch):
    """会话末巩固只抽游标之后的尾段——两通道不重复烧 token。"""
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 100)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 2)
    m = _mgr(tmp_path)
    all_msgs = [{"role": "user", "content": "早期消息"}, {"role": "user", "content": "第二句"}]
    m.update_short_term(MSGS, all_messages=all_msgs)
    m.update_short_term(MSGS, all_messages=all_msgs)   # 第 2 轮触发抽取,游标=2
    calls_before = len(m.client.calls)
    all_msgs.append({"role": "user", "content": "尾段新消息"})
    m.consolidate_to_long_term(all_msgs, None)
    tail_call = str(m.client.calls[-1])
    assert "尾段新消息" in tail_call and "早期消息" not in tail_call
    assert len(m.client.calls) == calls_before + 1


# ---- 观察1修复:后台高频调用用短超时 bg_client,consolidate 用主 client ----
def test_stm_and_checkpoint_use_bg_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 1)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 2)
    monkeypatch.setattr(settings, "memory_async_updates", False)
    main, bg = FakeClient(), FakeClient()
    m = MemoryManager(client=main, model="m", user_id="u1",
                      memory_dir=str(tmp_path / "mem"), memory_enabled=True, bg_client=bg)
    all_msgs = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    m.update_short_term(MSGS, all_messages=all_msgs)   # 轮1:STM
    m.update_short_term(MSGS, all_messages=all_msgs)   # 轮2:STM + checkpoint
    assert len(bg.calls) >= 2 and main.calls == []      # 高频后台全走 bg,主 client 未被碰


def test_bg_client_defaults_to_main_when_absent(tmp_path, monkeypatch):
    """不传 bg_client 时回退主 client——向后兼容,既有测试行为不变。"""
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 1)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    monkeypatch.setattr(settings, "memory_async_updates", False)
    main = FakeClient()
    m = MemoryManager(client=main, model="m", user_id="u1",
                      memory_dir=str(tmp_path / "mem"), memory_enabled=True)
    m.update_short_term(MSGS, all_messages=MSGS)
    assert len(main.calls) == 1 and m._bg_client is main


def test_consolidate_keeps_main_client(tmp_path, monkeypatch):
    """会话末巩固是一次性重要操作,保留强容错主 client(不走短超时 bg)。"""
    monkeypatch.setattr(settings, "memory_async_updates", False)
    main, bg = FakeClient(), FakeClient()
    m = MemoryManager(client=main, model="m", user_id="u1",
                      memory_dir=str(tmp_path / "mem"), memory_enabled=True, bg_client=bg)
    m.consolidate_to_long_term([{"role": "user", "content": "记住我喜欢红色"}], None)
    assert len(main.calls) == 1 and bg.calls == []


def test_ecomagent_builds_short_timeout_zero_retry_bg_client(tmp_path, monkeypatch):
    """真 EcomAgent 构造出的 bg_client 是短超时、零重试(不触网,只查对象属性)。"""
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    monkeypatch.setattr(settings, "memory_bg_timeout_s", 42.0)
    monkeypatch.setattr(settings, "resilience_enabled", False)
    from app.agent.chat import EcomAgent
    a = EcomAgent(session_path=str(tmp_path / "s.json"), user_id="u1")
    bg = a.memory_manager._bg_client
    assert bg is not a.memory_manager.client          # 独立于主 client
    assert bg.max_retries == 0
    assert float(getattr(bg, "timeout", 0)) == 42.0
