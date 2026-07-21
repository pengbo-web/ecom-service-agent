"""Phase 5:长期记忆 LLM 策展(合并/纠正/按重要性淘汰)+ 失败降级。fake client,不触网。"""

import app.agent.memory.long_term as ltm_mod
from app.agent.memory.curation import curate_facts, _strip_code_fence
from app.agent.memory.long_term import MemoryFact, LongTermMemory


class _Completions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls += 1
        if self.outer.raise_exc:
            raise RuntimeError("boom")
        msg = type("M", (), {"content": self.outer.content})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


class FakeClient:
    def __init__(self, content="", raise_exc=False):
        self.content = content
        self.raise_exc = raise_exc
        self.calls = 0
        self.chat = type("Chat", (), {"completions": _Completions(self)})()


def _fact(content, category="preference", created_at="2024-01-01T00:00:00"):
    return MemoryFact(content=content, category=category, created_at=created_at)


# ---- curate_facts 单元 ----
def test_curate_merges_near_duplicates():
    client = FakeClient('{"facts":[{"content":"偏好红色系商品","category":"preference"}]}')
    out = curate_facts(client, "m",
                       [_fact("喜欢红色")], [_fact("偏好红色款式")], max_facts=50)
    assert len(out) == 1 and out[0].content == "偏好红色系商品"


def test_curate_preserves_created_at_for_unchanged():
    client = FakeClient(
        '{"facts":[{"content":"是钻石会员","category":"identity"},'
        '{"content":"喜欢运动鞋","category":"preference"}]}')
    existing = [_fact("是钻石会员", "identity", "2023-05-05T10:00:00")]
    out = curate_facts(client, "m", existing, [_fact("喜欢运动鞋")], max_facts=50)
    kept = {f.content: f for f in out}
    assert kept["是钻石会员"].created_at == "2023-05-05T10:00:00"   # 老事实保留原年龄
    assert kept["喜欢运动鞋"].created_at != ""                       # 新事实给当前时间


def test_curate_truncates_to_max_facts():
    client = FakeClient(
        '{"facts":[{"content":"a"},{"content":"b"},{"content":"c"}]}')
    out = curate_facts(client, "m", [], [_fact("x")], max_facts=2)
    assert len(out) == 2


def test_curate_dedups_repeated_content():
    client = FakeClient('{"facts":[{"content":"会员"},{"content":"会员"}]}')
    out = curate_facts(client, "m", [], [_fact("会员")], max_facts=50)
    assert len(out) == 1


def test_curate_returns_none_on_bad_json():
    assert curate_facts(FakeClient("这不是JSON"), "m", [], [_fact("x")], 50) is None


def test_curate_returns_none_on_exception():
    assert curate_facts(FakeClient(raise_exc=True), "m", [], [_fact("x")], 50) is None


def test_curate_returns_none_on_empty_result():
    # LLM 返回空清单 → 视为失败(None),避免误清空记忆
    assert curate_facts(FakeClient('{"facts":[]}'), "m", [_fact("x")], [], 50) is None


def test_curate_strips_code_fence():
    assert _strip_code_fence('```json\n{"facts":[]}\n```') == '{"facts":[]}'
    client = FakeClient('```json\n{"facts":[{"content":"会员"}]}\n```')
    out = curate_facts(client, "m", [], [_fact("会员")], 50)
    assert out and out[0].content == "会员"


# ---- extract_and_save 接线 + 降级 ----
def _patch_extraction(monkeypatch, new_facts):
    monkeypatch.setattr(ltm_mod, "extract_long_term_facts",
                        lambda *a, **k: (new_facts, "本次交互摘要"))


def test_extract_and_save_uses_curation_when_enabled(monkeypatch, tmp_path):
    _patch_extraction(monkeypatch, [_fact("喜欢红色款")])
    mem = LongTermMemory(memory_dir=str(tmp_path), curate_enabled=True)
    mem.facts = [_fact("喜欢红色")]
    client = FakeClient('{"facts":[{"content":"偏好红色系","category":"preference"}]}')
    mem.extract_and_save(client, "m", [{"role": "user", "content": "hi"}], None)
    assert [f.content for f in mem.facts] == ["偏好红色系"]   # 被合并为一条


def test_extract_and_save_falls_back_when_curation_fails(monkeypatch, tmp_path):
    _patch_extraction(monkeypatch, [_fact("喜欢蓝色")])
    mem = LongTermMemory(memory_dir=str(tmp_path), curate_enabled=True)
    mem.facts = [_fact("喜欢红色")]
    client = FakeClient("坏JSON")   # 策展失败 → 降级 add_facts
    mem.extract_and_save(client, "m", [{"role": "user", "content": "hi"}], None)
    contents = {f.content for f in mem.facts}
    assert contents == {"喜欢红色", "喜欢蓝色"}   # 两条都在(朴素追加)


def test_extract_and_save_naive_when_curation_disabled(monkeypatch, tmp_path):
    _patch_extraction(monkeypatch, [_fact("喜欢蓝色")])
    mem = LongTermMemory(memory_dir=str(tmp_path), curate_enabled=False)
    mem.facts = [_fact("喜欢红色")]
    client = FakeClient('{"facts":[{"content":"X"}]}')
    mem.extract_and_save(client, "m", [{"role": "user", "content": "hi"}], None)
    assert client.calls == 0                       # 未启用策展 → 根本不调 LLM 策展
    assert {f.content for f in mem.facts} == {"喜欢红色", "喜欢蓝色"}
