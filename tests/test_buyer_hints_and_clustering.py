"""跨 Agent 经验共享的买家侧落地 + 自进化闭环的语义聚类。

两条都是「把已有能力接上」,但第一条带一个**必须守住的安全边界**:参谋的诊断
是写给店主的经营数据,不能原样进买家上下文。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agent.skills import clustering, synthesizer
from app.config.settings import settings
from app.multi_agent.buyer_hints import hint_for, render_buyer_hints


def _entry(subject: str, kind: str, conclusion: str = "退款率 30%,超告警线 15%"):
    return {"key": f"diagnosis:{subject}",
            "value": {"subject": subject, "kind": kind, "conclusion": conclusion,
                      "subject_name": "跑鞋", "degraded": False}}


# ---------- 买家侧提示:安全边界 ----------

def test_no_internal_numbers_ever_reach_the_buyer_prompt():
    """**本组最重要的一条。**

    诊断里的 conclusion 是 LLM 写给店主的经营判断(含退款率、告警线等具体
    指标)。渲染出的买家侧提示必须**一个数字都不带**、不含结论原文——泄露风险
    要从"靠模型自觉不说"变成"结构上没有可说的东西"。

    这个项目自己实跑验证过:prompt 里的禁令保得住动作、保不住话术。所以这条
    边界不能靠"不要透露"那句话,只能靠不注入。
    """
    block = render_buyer_hints([_entry("P001", "refund_rate_high")], "P001")
    assert block                                  # 确实注入了提示
    assert "30%" not in block and "15%" not in block
    assert "退款率" not in block                   # 连内部指标名都不出现
    assert "告警线" not in block


def test_product_hint_only_shows_for_the_product_being_asked_about():
    """顾客问 A 商品,不该带着 B 商品的注意事项去回答——那只会让语气无端变形。"""
    entries = [_entry("P001", "refund_rate_high"), _entry("P999", "bad_review_rate_high")]
    block = render_buyer_hints(entries, "P001")
    assert "退换反馈偏多" in block                  # P001 的
    assert "负面反馈" not in block                  # P999 的不该出现


def test_shop_level_hint_always_injected():
    """店铺级诊断(subject='shop')与在看哪个商品无关,恒注入。"""
    entries = [_entry("shop", "angry_rate_high")]
    assert "情绪偏激烈" in render_buyer_hints(entries, None)
    assert "情绪偏激烈" in render_buyer_hints(entries, "P001")


def test_unknown_kind_yields_no_hint():
    """未登记的异常类型不给泛化提示——一条没有真实依据的"注意点什么"
    只会让客服的语气莫名其妙地变谨慎。"""
    assert hint_for("brand_new_kind") == ""
    assert render_buyer_hints([_entry("shop", "brand_new_kind")], None) == ""


def test_malformed_entry_is_skipped_not_fatal():
    entries = [{"value": "不是 dict"}, {"nope": 1}, _entry("shop", "angry_rate_high")]
    assert "情绪偏激烈" in render_buyer_hints(entries, None)


def test_duplicate_hints_are_collapsed():
    """同一商品既有退款诊断又有别的,若文案相同只留一条,不刷屏。"""
    entries = [_entry("P1", "tool_error_rate_high"), _entry("shop", "tool_error_rate_high")]
    block = render_buyer_hints(entries, "P1")
    assert block.count("查询失败") == 1


def test_orchestrator_injects_hints_into_buyer_prompt(monkeypatch):
    """端到端:买家总控组装 system prompt 时带上了提示。"""
    from app.multi_agent import orchestrator as orch

    o = orch.MultiAgentOrchestrator.__new__(orch.MultiAgentOrchestrator)
    monkeypatch.setattr("app.multi_agent.shared_context.recent_entries",
                        lambda *a, **k: [_entry("shop", "angry_rate_high")])
    monkeypatch.setattr("app.agent.runtime_context.get_current_item", lambda: None)
    assert "情绪偏激烈" in o._buyer_hints_block()


def test_hint_injection_is_fail_soft(monkeypatch):
    """这是买家会话的热路径:提示读不出来绝不能让这一轮对话失败。"""
    from app.multi_agent import orchestrator as orch

    o = orch.MultiAgentOrchestrator.__new__(orch.MultiAgentOrchestrator)

    def _boom(*a, **k):
        raise RuntimeError("读库炸了")

    monkeypatch.setattr("app.multi_agent.shared_context.recent_entries", _boom)
    assert o._buyer_hints_block() == ""          # 不抛


# ---------- 语义聚类 ----------

def _fake_vectors(mapping: dict[str, list[float]]):
    """按文本给定向量,避免测试真打 embedding 端点。"""
    def _embed(texts):
        return [mapping[t] for t in texts]
    return _embed


def test_semantic_clustering_groups_by_meaning_not_keywords(monkeypatch):
    """关键词表的核心缺陷:「鞋子挤脚想换大一码」不含"退货/退款",落不进 refund 桶。
    语义聚类应该把它和「我要退货」聚到一起。"""
    monkeypatch.setattr(settings, "skill_semantic_clustering_enabled", True)
    texts = ["我要退货", "鞋子挤脚想换大一码", "快递到哪了"]
    monkeypatch.setattr(clustering, "_embed_all", _fake_vectors({
        "我要退货": [1.0, 0.0],
        "鞋子挤脚想换大一码": [0.95, 0.31],      # 与"退货"余弦 ≈0.95
        "快递到哪了": [0.0, 1.0],
    }))
    clusters = clustering.cluster_texts(texts)
    assert sorted(len(c) for c in clusters) == [1, 2]
    退货簇 = next(c for c in clusters if len(c) == 2)
    assert set(退货簇) == {0, 1}


def test_clustering_returns_none_when_switch_off(monkeypatch):
    monkeypatch.setattr(settings, "skill_semantic_clustering_enabled", False)
    assert clustering.cluster_texts(["a", "b"]) is None


def test_clustering_returns_none_when_embedding_fails(monkeypatch):
    """embedding 挂了返回 None,调用方据此回落关键词——自进化是离线增强,
    不该因为向量这一步挂了就整个跑不动。"""
    monkeypatch.setattr(settings, "skill_semantic_clustering_enabled", True)
    monkeypatch.setattr(clustering, "_embed_all", lambda _t: None)
    assert clustering.cluster_texts(["a"]) is None


def test_centroid_does_not_drift_so_results_are_reproducible(monkeypatch):
    """簇心用首条成员而非动态平均:平均会让簇心随并入顺序漂移,同一批样本换个
    顺序就聚出不同结果——离线合成的输入端必须可复现。"""
    monkeypatch.setattr(settings, "skill_semantic_clustering_enabled", True)
    vecs = {"a": [1.0, 0.0], "b": [0.9, 0.44], "c": [0.8, 0.6]}
    monkeypatch.setattr(clustering, "_embed_all", _fake_vectors(vecs))
    first = clustering.cluster_texts(["a", "b", "c"])
    monkeypatch.setattr(clustering, "_embed_all", _fake_vectors(vecs))
    again = clustering.cluster_texts(["a", "b", "c"])
    assert first == again


def test_group_samples_falls_back_to_keywords(monkeypatch):
    """embedding 不可用时回落关键词,并且**记 warning 不静默**——否则
    "为什么这次聚类结果变差了"查不出来。"""
    monkeypatch.setattr(synthesizer, "cluster_texts", lambda _t: None)
    archived = [{"messages": [{"role": "user", "content": "我要退款"}]},
                {"messages": [{"role": "user", "content": "快递到哪了"}]}]
    groups = synthesizer.group_samples(archived)
    assert set(groups) == {"refund", "logistics"}


def test_group_samples_uses_semantic_clusters_when_available(monkeypatch):
    """分组由语义决定,关键词只负责给簇起个可读的名字。"""
    monkeypatch.setattr(synthesizer, "cluster_texts", lambda _t: [[0, 1], [2]])
    archived = [{"messages": [{"role": "user", "content": "我要退款"}]},
                {"messages": [{"role": "user", "content": "鞋子挤脚想换大一码"}]},
                {"messages": [{"role": "user", "content": "快递到哪了"}]}]
    groups = synthesizer.group_samples(archived)
    assert len(groups["refund"]) == 2          # 语义把两条聚到了一起
    assert len(groups["logistics"]) == 1


def test_same_keyword_two_semantic_clusters_get_distinct_labels(monkeypatch):
    """两个不同语义簇撞同一个关键词名时加序号,不能互相覆盖——覆盖会让一整簇
    样本凭空消失。"""
    monkeypatch.setattr(synthesizer, "cluster_texts", lambda _t: [[0], [1]])
    archived = [{"messages": [{"role": "user", "content": "我要退款"}]},
                {"messages": [{"role": "user", "content": "退款到账了吗"}]}]
    groups = synthesizer.group_samples(archived)
    assert set(groups) == {"refund", "refund-2"}
    assert sum(len(v) for v in groups.values()) == 2      # 一条都没丢
