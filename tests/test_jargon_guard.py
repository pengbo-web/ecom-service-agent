"""L4:出话内部黑话检测——纯函数命中判定 + EcomAgent 旁路埋点(fail-soft)。"""

from types import SimpleNamespace

from app.agent import jargon_guard as jg
from app.agent.chat import EcomAgent
from app.agent.skills.loader import SkillManager


def test_detects_skill_name_and_internal_jargon():
    text = ("根据当前已加载的「process-return」技能流程，我将按标准步骤为您办理："
            "第一步，请提供订单号，若您暂不确定，我也可以先调用工具列出您全部订单。")
    hits = jg.detect_internal_leak(text, ["process-return", "track-order"])
    assert "process-return" in hits
    assert "调用工具" in hits
    assert "技能流程" in hits


def test_clean_reply_has_no_hits():
    text = "我先帮您查一下订单，麻烦稍等，很快就有结果～"
    hits = jg.detect_internal_leak(text, ["process-return", "track-order"])
    assert hits == []


def test_empty_text_is_safe():
    assert jg.detect_internal_leak("", ["process-return"]) == []
    assert jg.detect_internal_leak(None, ["process-return"]) == []


def test_vocabulary_is_not_hand_copied_it_derives_from_skill_manager():
    """词表必须随 SkillManager.skill_names 变化,不是在 jargon_guard 里手抄
    第二份 skill 清单——本项目已经因为手抄表跟真源 drift 出过好几次问题。"""
    # jargon_guard 自己的固定词表只装内部黑话,不装任何具体 skill 名
    assert "process-return" not in jg.INTERNAL_JARGON_TERMS
    assert "track-order" not in jg.INTERNAL_JARGON_TERMS

    mgr = SkillManager(skills_dir="app/agent/skills/definitions", enabled=True)
    assert "process-return" in mgr.skill_names   # 真实技能目录确实注册了这个名字

    # 检测器把调用方传入的 skill_names(应取自 SkillManager)当作词表的一部分,
    # 命中的正是"当前真实存在"的技能名——不是靠某处写死的列表巧合对上。
    hits = jg.detect_internal_leak("已加载process-return的流程", mgr.skill_names)
    assert "process-return" in hits


def test_check_internal_leak_emits_observability_event_on_hit():
    """EcomAgent._check_internal_leak:命中就发观测事件,不改写/不阻断回复。"""
    events = []
    fake_self = SimpleNamespace(
        skill_manager=SimpleNamespace(skill_names=["process-return"]),
        _emit=lambda e: events.append(e),
    )
    text = "已加载process-return技能流程，我先调用工具查一下"

    EcomAgent._check_internal_leak(fake_self, text)

    assert len(events) == 1
    assert events[0]["type"] == "guard"
    assert events[0]["kind"] == "internal_leak"
    assert "process-return" in events[0]["hits"]


def test_check_internal_leak_silent_on_clean_reply():
    events = []
    fake_self = SimpleNamespace(
        skill_manager=SimpleNamespace(skill_names=["process-return"]),
        _emit=lambda e: events.append(e),
    )
    EcomAgent._check_internal_leak(fake_self, "我先帮您查一下订单")
    assert events == []


def test_check_internal_leak_is_fail_soft_never_raises():
    """旁路埋点:检测/发射过程中任何异常都必须被吞掉,绝不能影响本轮回复。"""
    fake_self = SimpleNamespace(
        skill_manager=None,  # 故意制造异常路径(访问 .skill_names 会失败)
        _emit=lambda e: (_ for _ in ()).throw(RuntimeError("sink 挂了")),
    )
    # 不应抛出任何异常
    EcomAgent._check_internal_leak(fake_self, "已加载process-return技能流程")
