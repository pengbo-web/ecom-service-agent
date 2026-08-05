"""R2:步级 checkpoint(每工具步落盘 + in_flight 标记)+ L1 中断恢复。"""

import json
import types

import pytest

from app.agent.chat import EcomAgent
from app.session.store import set_session_store
from app.config.settings import settings


class RecordingStore:
    """记录每次 save 的状态快照(供断言 checkpoint)。"""
    def __init__(self, initial=None):
        self.saves = []
        self._initial = initial
    def load(self, key):
        return self._initial
    def save(self, key, state):
        self.saves.append(json.loads(json.dumps(state)))   # 深拷贝快照
    def delete(self, key):
        pass


class FakeTM:
    tool_definitions: list = []
    def execute_tool(self, name, args):
        return json.dumps({"success": True})


def _bare_agent(store):
    """跳过重 __init__,只装 _execute_tool_call / _checkpoint 需要的字段。"""
    a = EcomAgent.__new__(EcomAgent)
    a.session_path = "x.json"; a.session_id = "x"
    a.store = store
    a.raw_messages = []
    a.summary = None
    a._status = "complete"; a._step_seq = 0
    a._pending = None
    a.event_sink = lambda e: None
    a.tool_manager = FakeTM()
    a.memory_manager = types.SimpleNamespace(stm_to_dict=lambda: {})
    return a


# ---- checkpoint 写入 ----
def test_checkpoint_writes_in_flight_with_step_seq():
    store = RecordingStore()
    a = _bare_agent(store)
    a.raw_messages.append({"role": "assistant", "content": None,
                           "tool_calls": [{"id": "c1", "type": "function",
                                           "function": {"name": "query_order", "arguments": "{}"}}]})
    a._execute_tool_call("c1", "query_order", {"order_id": "O1"})
    assert a._step_seq == 1
    last = store.saves[-1]
    assert last["status"] == "in_flight" and last["step_seq"] == 1
    # 工具结果已进历史(与 assistant tool_calls 成对)
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c1" for m in last["messages"])


def test_checkpoint_disabled_skips_step_writes(monkeypatch):
    monkeypatch.setattr(settings, "checkpoint_enabled", False)
    store = RecordingStore()
    a = _bare_agent(store)
    a._execute_tool_call("c1", "query_order", {"order_id": "O1"})
    assert store.saves == []          # 关闭时不做步级落盘
    assert a._step_seq == 1           # 计数仍推进


# ---- L1 中断恢复 ----
def test_recovery_from_in_flight_heals_orphan(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    # 预置"回合中途崩溃"态:assistant 有 tool_call 但缺 tool 结果(孤儿)
    interrupted = {
        "version": 1, "summary": None, "short_term_memory": None,
        "status": "in_flight", "step_seq": 1,
        "messages": [
            {"role": "user", "content": "查订单 O1"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "query_order", "arguments": "{}"}}]},
        ],
    }
    set_session_store(RecordingStore(initial=interrupted))
    a = EcomAgent(session_path=str(tmp_path / "s.json"), session_id="s", user_id="u")

    # 上下文恢复:用户消息还在
    assert a.raw_messages[0]["content"] == "查订单 O1"
    assert a._step_seq == 1
    assert a._status == "complete"                 # in_flight 修复后置为可继续
    # 孤儿被修复:回填了缺失的 tool 结果(每个 tool_call 现在有对应 tool 消息)
    assert any(m.get("role") == "tool" for m in a.raw_messages)


def test_recovery_complete_session_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    normal = {
        "version": 1, "summary": None, "short_term_memory": None,
        "status": "complete", "step_seq": 0,
        "messages": [{"role": "user", "content": "在吗"},
                     {"role": "assistant", "content": json.dumps({"reply": "在的"})}],
    }
    set_session_store(RecordingStore(initial=normal))
    a = EcomAgent(session_path=str(tmp_path / "s.json"), session_id="s", user_id="u")
    assert len(a.raw_messages) == 2 and a._status == "complete"   # 正常会话不被改动


# ---- 缺字段的存量会话:不应崩溃(Bug 1 回归) ----
def test_recovery_missing_summary_key_defaults_to_none(monkeypatch, tmp_path):
    """存量会话缺 summary 键(而非 summary=None)——曾经的裸 loaded["summary"] 会 KeyError。"""
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    missing_summary = {
        "version": 1, "short_term_memory": None,
        "status": "complete", "step_seq": 0,
        "messages": [{"role": "user", "content": "在吗"}],
        # 注意:没有 "summary" 键
    }
    set_session_store(RecordingStore(initial=missing_summary))
    a = EcomAgent(session_path=str(tmp_path / "s.json"), session_id="s", user_id="u")
    assert a.summary is None
    assert len(a.raw_messages) == 1


def test_recovery_missing_messages_key_defaults_to_empty(monkeypatch, tmp_path):
    """存量会话缺 messages 键——曾经的裸 loaded["messages"] 会 KeyError,应降级为空会话。"""
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    missing_messages = {
        "version": 1, "summary": "之前聊过退货", "short_term_memory": None,
        "status": "complete", "step_seq": 0,
        # 注意:没有 "messages" 键
    }
    set_session_store(RecordingStore(initial=missing_messages))
    a = EcomAgent(session_path=str(tmp_path / "s.json"), session_id="s", user_id="u")
    assert a.raw_messages == []
    assert a.summary == "之前聊过退货"
