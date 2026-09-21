"""WS1 生产者③:会话末 LTM 巩固捎带 skill_gap_note(技术方案 §2)。

守四条:开关默认关(不请求不解析)、捎带零额外 round trip(同一次调用)、
解析失败/非字符串当没有、标记落库 fail-soft 且只标记不判定。
"""

import json
from types import SimpleNamespace

import pytest

from app.agent.memory.extraction import extract_long_term_facts
from app.agent.memory.long_term import LongTermMemory
from app.agent.skills import memory_hints as mh
from app.config.settings import settings


class _FakeClient:
    def __init__(self, content):
        self.captured = []
        compl = SimpleNamespace()
        compl.create = lambda **kw: self._create(kw)
        self._content = content
        self.chat = SimpleNamespace(completions=compl)

    def _create(self, kw):
        self.captured.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self._content))])


_MSGS = [{"role": "user", "content": "退货运费谁出"},
         {"role": "assistant", "content": "运费由您承担"}]


def _payload(note=None):
    data = {"facts": [], "interaction_summary": "一轮咨询"}
    if note is not None:
        data["skill_gap_note"] = note
    return json.dumps(data, ensure_ascii=False)


def test_off_by_default_no_instruction_no_note():
    client = _FakeClient(_payload(note="客服答错了运费政策"))
    facts, summary, note = extract_long_term_facts(
        client, "m", _MSGS, None, [], piggyback_hint=False)
    assert note == ""
    assert "skill_gap_note" not in client.captured[0]["messages"][1]["content"]


def test_on_returns_note_and_injects_instruction():
    client = _FakeClient(_payload(note="客服答错了运费政策"))
    facts, summary, note = extract_long_term_facts(
        client, "m", _MSGS, None, [], piggyback_hint=True)
    assert note == "客服答错了运费政策"
    assert "skill_gap_note" in client.captured[0]["messages"][1]["content"]
    assert len(client.captured) == 1          # 捎带:零额外 round trip


def test_note_truncated_and_non_string_ignored():
    client = _FakeClient(_payload(note="长" * 100))
    _, _, note = extract_long_term_facts(
        client, "m", _MSGS, None, [], piggyback_hint=True)
    assert len(note) == 80
    client = _FakeClient(_payload(note={"not": "a string"}))
    _, _, note = extract_long_term_facts(
        client, "m", _MSGS, None, [], piggyback_hint=True)
    assert note == ""


def test_parse_failure_yields_empty_note_not_raise():
    client = _FakeClient("这不是 JSON")
    facts, summary, note = extract_long_term_facts(
        client, "m", _MSGS, None, [], piggyback_hint=True)
    assert (facts, summary, note) == ([], "（提取失败）", "")


@pytest.fixture()
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(mh, "record_hint",
                        lambda sid, skill, kind, **kw: calls.append((sid, skill, kind, kw))
                        and "h1")
    return calls


def _ltm(tmp_path):
    return LongTermMemory(user_id="u1", memory_dir=str(tmp_path), curate_enabled=False)


def test_extract_and_save_records_piggyback_hint(tmp_path, captured):
    ltm = _ltm(tmp_path)
    ltm.extract_and_save(_FakeClient(_payload(note="该查没查就直接答了")), "m",
                         _MSGS, None, session_id="s1", skill_name="track-order",
                         piggyback_hint=True)
    assert len(captured) == 1
    sid, skill, kind, kw = captured[0]
    assert (sid, skill, kind) == ("s1", "track-order", mh.KIND_PIGGYBACK_NOTE)
    assert kw["source"] == mh.SOURCE_LLM_PIGGYBACK


def test_default_flag_off_means_no_hint(tmp_path, captured):
    assert settings.skill_hint_piggyback_enabled is False
    ltm = _ltm(tmp_path)
    ltm.extract_and_save(_FakeClient(_payload(note="note")), "m", _MSGS, None,
                         session_id="s1", skill_name="track-order")
    assert captured == []


def test_flag_on_via_settings(tmp_path, captured, monkeypatch):
    monkeypatch.setattr(settings, "skill_hint_piggyback_enabled", True)
    ltm = _ltm(tmp_path)
    ltm.extract_and_save(_FakeClient(_payload(note="note")), "m", _MSGS, None,
                         session_id="s1", skill_name="track-order")
    assert len(captured) == 1


def test_hint_write_failure_does_not_break_consolidation(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("hints table down")
    monkeypatch.setattr(mh, "record_hint", boom)
    ltm = _ltm(tmp_path)
    ltm.extract_and_save(_FakeClient(_payload(note="note")), "m", _MSGS, None,
                         session_id="s1", skill_name="track-order",
                         piggyback_hint=True)   # 不许冒泡
