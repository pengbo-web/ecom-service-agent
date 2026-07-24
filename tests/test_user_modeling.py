"""H3.4：用户建模——从行为归纳偏好标签写入档案（fake client，不触网）。"""

from __future__ import annotations

import pytest

from app.agent.memory.profile import UserProfileStore, get_profile_store, set_profile_store
from app.agent.skills.user_modeling import model_user
from app.config.settings import settings


@pytest.fixture(autouse=True)
def _reset_profile_store_singleton():
    """每个测试前后复位全局单例，防跨测试泄漏。"""
    set_profile_store(None)
    yield
    set_profile_store(None)


class _FakeCompletions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls.append(kwargs)
        content = self.outer.script.pop(0)
        msg = type("M", (), {"content": content})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


class FakeClient:
    """脚本化 fake client：chat.completions.create(...) 按调用次序依次吐出预设内容。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.chat = type("Chat", (), {"completions": _FakeCompletions(self)})()


def _sample(pairs, summary="", user_id="u1"):
    return {
        "messages": [{"role": r, "content": c} for r, c in pairs],
        "summary": summary,
        "user_id": user_id,
    }


SAMPLES = [
    _sample([("user", "有没有红色的鞋子"), ("assistant", "有的，为您推荐")]),
    _sample([("user", "太贵了，有没有便宜点的"), ("assistant", "帮您看看优惠")]),
]

TAGS_JSON = '["偏好红色","价格敏感"]'
BAD_JSON = "这不是 JSON，是 LLM 瞎说的一段话。"


# ---------- 1. 正常路径：返回标签且落库 ----------

def test_model_user_returns_tags_and_writes_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", True)
    store = UserProfileStore(str(tmp_path / "profile.db"))

    client = FakeClient([TAGS_JSON])
    tags = model_user(client, "test-model", "u1", SAMPLES, store=store)

    assert tags == ["偏好红色", "价格敏感"]
    profile = store.get("u1")
    assert "偏好红色" in profile.tags
    assert "价格敏感" in profile.tags
    store.close()


# ---------- 2. 空样本：不调 LLM ----------

def test_model_user_empty_samples_returns_empty_no_calls(tmp_path):
    store = UserProfileStore(str(tmp_path / "profile.db"))
    client = FakeClient([])
    tags = model_user(client, "test-model", "u1", [], store=store)

    assert tags == []
    assert client.calls == []
    store.close()


# ---------- 3. 坏 LLM 输出：fail-soft ----------

def test_model_user_bad_output_returns_empty_no_crash(tmp_path):
    store = UserProfileStore(str(tmp_path / "profile.db"))
    client = FakeClient([BAD_JSON])
    tags = model_user(client, "test-model", "u1", SAMPLES, store=store)

    assert tags == []
    profile = store.get("u1")
    assert profile.tags == []
    store.close()


# ---------- 4. store=None 且门控关：只返回标签，不落库不炸 ----------

def test_model_user_store_none_gated_off_no_crash(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)
    set_profile_store(None)

    client = FakeClient([TAGS_JSON])
    tags = model_user(client, "test-model", "u1", SAMPLES, store=None)

    assert tags == ["偏好红色", "价格敏感"]
    assert get_profile_store() is None
