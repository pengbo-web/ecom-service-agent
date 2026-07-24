"""H3.5：三步离线闭环入口（①用户建模 ②聚类创建 ③失败自改进），fake client，不触网。

不重复测 synthesize_skills（H3.1 已覆盖），只测 H3.5 新增的四个函数：
is_failure_sample / related_failures / run_user_modeling / run_improvements。
"""

from __future__ import annotations

from app.agent.skills.loader import _parse_frontmatter
from app.config.settings import settings
from app.scripts.synthesize_skills import (
    is_failure_sample,
    related_failures,
    run_improvements,
    run_user_modeling,
)


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


# ============================================================
# is_failure_sample
# ============================================================

def test_is_failure_sample_handoff_keyword_true():
    s = _sample([("user", "退款怎么还没到账"), ("assistant", "我帮您转人工处理")])
    assert is_failure_sample(s) is True


def test_is_failure_sample_manual_service_keyword_true():
    s = _sample([("user", "这个问题解决不了"), ("assistant", "请联系人工客服处理")])
    assert is_failure_sample(s) is True


def test_is_failure_sample_apology_twice_true():
    s = _sample([("user", "退货流程太麻烦了"), ("assistant", "抱歉抱歉，给您带来不便")])
    assert is_failure_sample(s) is True


def test_is_failure_sample_apology_once_false():
    s = _sample([("user", "还没收到货"), ("assistant", "抱歉，正在为您加急处理")])
    assert is_failure_sample(s) is False


def test_is_failure_sample_summary_complaint_true():
    s = _sample([("user", "东西坏了"), ("assistant", "好的，帮您处理")], summary="用户投诉商品质量问题")
    assert is_failure_sample(s) is True


def test_is_failure_sample_summary_dissatisfied_true():
    s = _sample([("user", "东西坏了"), ("assistant", "好的，帮您处理")], summary="用户对处理结果不满")
    assert is_failure_sample(s) is True


def test_is_failure_sample_normal_false():
    s = _sample([("user", "我的快递到哪了"), ("assistant", "正在路上，预计明天送达")])
    assert is_failure_sample(s) is False


# ============================================================
# related_failures
# ============================================================

REFUND_DESC = "当用户想退款/退货时使用的快速处理流程。"

REFUND_FAILURE = _sample(
    [("user", "退款流程太复杂了，一直没处理"), ("assistant", "我帮您转人工")],
)
LOGISTICS_FAILURE = _sample(
    [("user", "快递一直没更新，太慢了"), ("assistant", "我帮您转人工")],
)


def test_related_failures_matches_by_shared_keyword():
    result = related_failures(
        "refund-fast-track", REFUND_DESC, [REFUND_FAILURE, LOGISTICS_FAILURE],
    )
    assert result == [REFUND_FAILURE]


def test_related_failures_no_match_returns_empty():
    result = related_failures("refund-fast-track", REFUND_DESC, [LOGISTICS_FAILURE])
    assert result == []


def test_related_failures_empty_failures_returns_empty():
    assert related_failures("refund-fast-track", REFUND_DESC, []) == []


# ============================================================
# run_user_modeling
# ============================================================

TAGS_JSON_U1 = '["偏好红色"]'
TAGS_JSON_U2 = '["价格敏感"]'


def test_run_user_modeling_two_users_modeled(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)  # 不落库，只看返回值/调用次数

    archives = [
        _sample([("user", "有没有红色的鞋子"), ("assistant", "有的")], user_id="u1"),
        _sample([("user", "太贵了"), ("assistant", "帮您看看")], user_id="u1"),
        _sample([("user", "物流到哪了"), ("assistant", "在路上")], user_id="u2"),
        _sample([("user", "快递好慢"), ("assistant", "催一下")], user_id="u2"),
    ]
    client = FakeClient([TAGS_JSON_U1, TAGS_JSON_U2])
    result = run_user_modeling(client, "test-model", archives)

    assert result == {"u1": ["偏好红色"], "u2": ["价格敏感"]}
    assert len(client.calls) == 2


def test_run_user_modeling_skips_single_sample_user(monkeypatch):
    monkeypatch.setattr(settings, "memory_profile_enabled", False)

    archives = [
        _sample([("user", "有没有红色的鞋子"), ("assistant", "有的")], user_id="u1"),
        _sample([("user", "太贵了"), ("assistant", "帮您看看")], user_id="u1"),
        _sample([("user", "只问一句")], user_id="u3"),  # 单条样本，跳过不建模
    ]
    client = FakeClient([TAGS_JSON_U1])
    result = run_user_modeling(client, "test-model", archives)

    assert set(result.keys()) == {"u1"}
    assert len(client.calls) == 1


# ============================================================
# run_improvements
# ============================================================

REFUND_SKILL_MD = """---
name: refund-fast-track
description: 当用户想退款/退货时使用的快速处理流程。
---

## 处理流程

### 第一步
确认订单与退款原因。
"""

IMPROVED_REFUND_SKILL_MD = """---
name: refund-fast-track
description: 当用户想退款/退货时使用的快速处理流程（已根据失败案例改进）。
---

## 处理流程

### 第一步
确认订单与退款原因，特别注意用户情绪激动时先安抚。
"""


def _write_refund_skill(skills_dir):
    skill_dir = skills_dir / "refund-fast-track"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(REFUND_SKILL_MD, encoding="utf-8")


def test_run_improvements_produces_candidate(tmp_path):
    skills_dir = tmp_path / "definitions"
    _write_refund_skill(skills_dir)
    out_dir = tmp_path / "_candidates"

    archives = [
        REFUND_FAILURE,  # 含"转人工" → is_failure_sample=True，且与 refund skill 相关
        _sample([("user", "快递好慢"), ("assistant", "已催促物流")]),  # 正常样本，非失败
    ]
    client = FakeClient([IMPROVED_REFUND_SKILL_MD])
    out_paths = run_improvements(client, "test-model", archives, str(skills_dir), str(out_dir))

    assert len(out_paths) == 1
    text = out_paths[0].read_text(encoding="utf-8")
    meta = _parse_frontmatter(text)
    assert meta["name"] == "refund-fast-track"
    assert len(client.calls) == 1


def test_run_improvements_no_related_failure_returns_empty_no_calls(tmp_path):
    skills_dir = tmp_path / "definitions"
    _write_refund_skill(skills_dir)
    out_dir = tmp_path / "_candidates"

    archives = [
        LOGISTICS_FAILURE,  # 是失败样本，但与 refund skill 不相关
    ]
    client = FakeClient([])
    out_paths = run_improvements(client, "test-model", archives, str(skills_dir), str(out_dir))

    assert out_paths == []
    assert client.calls == []


def test_run_improvements_no_failure_samples_returns_empty_no_calls(tmp_path):
    skills_dir = tmp_path / "definitions"
    _write_refund_skill(skills_dir)
    out_dir = tmp_path / "_candidates"

    archives = [
        _sample([("user", "退款怎么还没到账"), ("assistant", "已经在处理了")]),  # 非失败样本
    ]
    client = FakeClient([])
    out_paths = run_improvements(client, "test-model", archives, str(skills_dir), str(out_dir))

    assert out_paths == []
    assert client.calls == []
