"""H2.3/G2 结构化用户档案测试(base/行为标签/工单流转)。

全离线,单例通过每个测试内 fixture 注入/复位,不改 tests/conftest.py。
"""

from __future__ import annotations

import pytest

from app.agent.memory.profile import (
    UserProfile,
    UserProfileStore,
    get_profile_store,
    record_ticket,
    set_profile_store,
)
from app.agent.memory.manager import MemoryManager
from app.config.settings import settings


@pytest.fixture(autouse=True)
def _reset_profile_store_singleton():
    """每个测试前后复位全局单例,防跨测试泄漏。"""
    set_profile_store(None)
    yield
    set_profile_store(None)


# ---------- 1. CRUD ----------

def test_update_base_then_get_readback(tmp_path):
    store = UserProfileStore(str(tmp_path / "profile.db"))
    store.update_base("u1", {"member_level": "钻石会员"})
    profile = store.get("u1")
    assert profile.base["member_level"] == "钻石会员"
    store.close()


def test_add_tag_dedup(tmp_path):
    store = UserProfileStore(str(tmp_path / "profile.db"))
    store.add_tag("u1", "偏好红色")
    store.add_tag("u1", "偏好红色")
    profile = store.get("u1")
    assert profile.tags == ["偏好红色"]
    store.close()


def test_add_ticket_then_get_has_full_fields(tmp_path):
    store = UserProfileStore(str(tmp_path / "profile.db"))
    store.add_ticket("u1", "h1", "escalated", "投诉物流太慢")
    profile = store.get("u1")
    assert len(profile.tickets) == 1
    t = profile.tickets[0]
    assert t["ticket_id"] == "h1"
    assert t["status"] == "escalated"
    assert t["reason"] == "投诉物流太慢"
    assert t["ts"]
    store.close()


def test_get_no_record_returns_empty_profile(tmp_path):
    store = UserProfileStore(str(tmp_path / "profile.db"))
    profile = store.get("no_such_user")
    assert profile.user_id == "no_such_user"
    assert profile.base == {}
    assert profile.tags == []
    assert profile.tickets == []
    store.close()


# ---------- 2. to_prompt ----------

def test_to_prompt_all_empty_returns_none():
    profile = UserProfile(user_id="u1", base={}, tags=[], tickets=[])
    assert profile.to_prompt() is None


def test_to_prompt_with_base_tags_tickets():
    profile = UserProfile(
        user_id="u1",
        base={"member_level": "钻石会员", "name": "张三"},
        tags=["偏好红色", "价格敏感"],
        tickets=[
            {"ticket_id": "h1", "status": "escalated", "reason": "投诉物流太慢",
             "ts": "2026-07-24T10:00:00"},
        ],
    )
    text = profile.to_prompt()
    assert text is not None
    assert "结构化档案" in text
    assert "member_level=钻石会员" in text
    assert "偏好红色" in text
    assert "价格敏感" in text
    assert "[escalated]" in text
    assert "投诉物流太慢" in text
    assert "2026-07-24T10:00:00" in text


def test_to_prompt_tickets_only_recent_3(tmp_path):
    store = UserProfileStore(str(tmp_path / "profile.db"))
    for i in range(4):
        store.add_ticket("u1", f"h{i}", "escalated", f"reason{i}")
    profile = store.get("u1")
    text = profile.to_prompt()
    assert "reason0" not in text
    assert "reason1" in text
    assert "reason2" in text
    assert "reason3" in text
    store.close()


# ---------- 3. manager 注入 ----------

class _FakeClient:
    """占位 client,注入路径不应调 LLM。"""


def test_manager_injects_profile_section(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", True)
    store = UserProfileStore(str(tmp_path / "profile.db"))
    store.update_base("u1", {"member_level": "钻石会员"})
    store.add_tag("u1", "偏好红色")
    set_profile_store(store)

    manager = MemoryManager(
        client=_FakeClient(), model="fake-model", user_id="u1",
        memory_dir=str(tmp_path / "mem"),
    )
    sections = manager.build_memory_prompt_sections()
    joined = "\n".join(s["content"] for s in sections)
    assert "结构化档案" in joined
    assert "member_level=钻石会员" in joined


def test_manager_gated_off_no_profile_section(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    store = UserProfileStore(str(tmp_path / "profile.db"))
    store.update_base("u1", {"member_level": "钻石会员"})
    set_profile_store(store)

    manager = MemoryManager(
        client=_FakeClient(), model="fake-model", user_id="u1",
        memory_dir=str(tmp_path / "mem"),
    )
    sections = manager.build_memory_prompt_sections()
    joined = "\n".join(s["content"] for s in sections)
    assert "结构化档案" not in joined


# ---------- 4. record_ticket best-effort ----------

def test_record_ticket_user_id_none_no_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", True)
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    record_ticket(None, "h1", "escalated", "投诉")  # 不应抛异常


def test_record_ticket_gated_off_store_none_no_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    record_ticket("u1", "h1", "escalated", "投诉")  # store None,不应抛异常


def test_record_ticket_normal_path_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", True)
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    record_ticket("u1", "h1", "escalated", "投诉物流太慢")

    store = get_profile_store()
    assert store is not None
    profile = store.get("u1")
    assert len(profile.tickets) == 1
    assert profile.tickets[0]["status"] == "escalated"


# ---------- 6. 升级落工单(集成/等价单测) ----------
# 说明:直接构造带 user_id 的 fake agent 调用 record_ticket 等价验证 C 段逻辑
# (streaming.py 中 `record_ticket(getattr(agent, "user_id", None), hid, "escalated", ...)`),
# 避免重驱动 test_streaming_hitl.py 里完整的 escalate 流式管线成本。

class _FakeAgentWithUserId:
    user_id = "u_escalate"


def test_escalate_records_ticket_via_agent_user_id(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", True)
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))

    agent = _FakeAgentWithUserId()
    hid = "h_esc_1"
    reasons = ["low_confidence", "explicit_request"]
    record_ticket(getattr(agent, "user_id", None), hid, "escalated", ";".join(reasons))

    store = get_profile_store()
    profile = store.get("u_escalate")
    assert len(profile.tickets) == 1
    assert profile.tickets[0]["ticket_id"] == hid
    assert profile.tickets[0]["status"] == "escalated"
    assert "low_confidence" in profile.tickets[0]["reason"]
