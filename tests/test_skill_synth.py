"""H3.1：会话聚类 + skill 合成器（fake client，不触网）。
H3.2：离线合成入口的 db 支撑方法 `list_recent_archives`。
"""

from app.agent.skills.loader import _parse_frontmatter, SkillManager
from app.agent.skills.synthesizer import group_samples, synthesize_one, synthesize_skills


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


REFUND_SAMPLES = [
    _sample([("user", "我要退货，鞋子不合脚"), ("assistant", "好的，帮您处理退货")]),
    _sample([("user", "这个东西想申请退款"), ("assistant", "已受理退款申请")]),
]

LOGISTICS_SAMPLES = [
    _sample([("user", "我的快递到哪了"), ("assistant", "正在路上")]),
    _sample([("user", "物流好久没更新了"), ("assistant", "帮您催一下")]),
]

REFUND_SKILL_MD = """---
name: refund-fast-track
description: 当用户想退款/退货时使用的快速处理流程。
---

## 处理流程

### 第一步
确认订单与退款原因。
"""

LOGISTICS_SKILL_MD = """---
name: logistics-stall-followup
description: 当用户问物流/快递/到哪了且物流停滞时使用。
---

## 处理流程

### 第一步
查询物流轨迹，判断是否停滞。
"""

BAD_SKILL_MD = "这是一段没有 frontmatter 的纯文本，LLM 没按要求输出。"


# ---------- group_samples ----------

def test_group_samples_classifies_refund_and_logistics():
    archived = REFUND_SAMPLES + LOGISTICS_SAMPLES
    groups = group_samples(archived)
    assert len(groups["refund"]) == 2
    assert len(groups["logistics"]) == 2


def test_group_samples_singleton_group_not_merged():
    archived = REFUND_SAMPLES + [_sample([("user", "随便问问，不属于任何关键词")])]
    groups = group_samples(archived)
    assert len(groups["refund"]) == 2
    assert len(groups["other"]) == 1


# ---------- synthesize_one ----------

def test_synthesize_one_returns_name_and_content():
    client = FakeClient([REFUND_SKILL_MD])
    result = synthesize_one(client, "test-model", REFUND_SAMPLES)
    assert result == {"name": "refund-fast-track", "content": REFUND_SKILL_MD}


def test_synthesize_one_bad_output_returns_none():
    client = FakeClient([BAD_SKILL_MD])
    result = synthesize_one(client, "test-model", REFUND_SAMPLES)
    assert result is None


# ---------- synthesize_skills ----------

def test_synthesize_skills_two_groups_produce_two_files(tmp_path):
    client = FakeClient([REFUND_SKILL_MD, LOGISTICS_SKILL_MD])
    archived = REFUND_SAMPLES + LOGISTICS_SAMPLES
    out_paths = synthesize_skills(client, "test-model", archived, str(tmp_path))

    assert len(out_paths) == 2
    names = set()
    for p in out_paths:
        assert p.exists()
        assert p.name == "SKILL.md"
        meta = _parse_frontmatter(p.read_text(encoding="utf-8"))
        assert meta.get("name")
        assert meta.get("description")
        names.add(meta["name"])
    assert names == {"refund-fast-track", "logistics-stall-followup"}


def test_synthesize_skills_bad_group_skipped_others_ok(tmp_path):
    # 第一组（refund）LLM 输出坏内容 → 跳过；第二组（logistics）正常
    client = FakeClient([BAD_SKILL_MD, LOGISTICS_SKILL_MD])
    archived = REFUND_SAMPLES + LOGISTICS_SAMPLES
    out_paths = synthesize_skills(client, "test-model", archived, str(tmp_path))

    assert len(out_paths) == 1
    meta = _parse_frontmatter(out_paths[0].read_text(encoding="utf-8"))
    assert meta["name"] == "logistics-stall-followup"


def test_synthesize_skills_empty_samples_returns_empty_no_calls(tmp_path):
    client = FakeClient([])
    out_paths = synthesize_skills(client, "test-model", [], str(tmp_path))
    assert out_paths == []
    assert client.calls == []


def test_synthesize_skills_skips_singleton_group(tmp_path):
    # "other" 组只有 1 条样本，不该被合成（不该消耗一次 LLM 调用）
    archived = REFUND_SAMPLES + [_sample([("user", "随便问问，不属于任何关键词")])]
    client = FakeClient([REFUND_SKILL_MD])
    out_paths = synthesize_skills(client, "test-model", archived, str(tmp_path))

    assert len(out_paths) == 1
    assert len(client.calls) == 1


# ---------- SkillManager 不加载候选 ----------

def test_skill_manager_does_not_load_candidates(tmp_path):
    definitions_dir = tmp_path / "definitions"
    real_skill_dir = definitions_dir / "track-order"
    real_skill_dir.mkdir(parents=True)
    (real_skill_dir / "SKILL.md").write_text(
        "---\nname: track-order\ndescription: 正式技能\n---\nbody", encoding="utf-8",
    )

    candidates_dir = definitions_dir / "_candidates"
    client = FakeClient([REFUND_SKILL_MD])
    written = synthesize_skills(client, "test-model", REFUND_SAMPLES, str(candidates_dir))
    assert len(written) == 1  # 候选确实写出去了

    sm = SkillManager(skills_dir=str(definitions_dir), enabled=True)
    assert "track-order" in sm.skill_names
    assert "refund-fast-track" not in sm.skill_names
    assert sm.skill_count == 1
