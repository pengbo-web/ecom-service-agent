"""协作链的三个管理端出口。

这三条补的是同一类缺陷:**能力早就写好了,但没有任何出口**。

- `routing.describe()` 的注释写着"供文档/管理端展示",此前零调用方;
- `collab.budget_status()` 的注释写着"供 /api/admin/collab/health 展示",
  此前零调用方;
- `list_events(correlation_id=...)` 要求调用方先知道 correlation_id,而在
  链清单之前没有任何地方列出过它——时间线端点事实上只对恰好失败的链开放。

和 `start_followup` 曾经零生产调用方是同一个形态:不报错、不告警,只是这部分
系统安静地不存在。所以这里钉的是**出口存在且口径唯一**,不只是"返回 200"。
"""

from __future__ import annotations

import pytest

from app.multi_agent import bus, routing


# ---------- 路由表的可读渲染 ----------

def test_describe_marks_dynamic_priority_instead_of_picking_one():
    """谓词式优先级给不出单一数值,必须如实标 dynamic。

    同一个 `signal.anomaly`,转人工是紧急、例行扫描是普通。管理端摆一个"普通"
    会让人以为转人工也排在普通队列里,而那正好是这条谓词存在的理由。
    """
    rows = {(r["event_type"], r["target"]): r for r in routing.describe()}
    anomaly = rows[("signal.anomaly", "analyst")]
    assert anomaly["priority_dynamic"] is True
    assert anomaly["priority"] is None, "动态优先级不该被压成一个具体数字"
    assert anomaly["priority_label"], "动态也要给人话说明,不能只给 None"

    diagnosis = rows[("insight.diagnosis", "growth")]
    assert diagnosis["priority_dynamic"] is False
    assert diagnosis["priority"] == routing.P_NORMAL
    assert diagnosis["priority_label"] == "普通"


def test_describe_keeps_conditional_flag_and_reason():
    rows = {(r["event_type"], r["target"]): r for r in routing.describe()}
    assert rows[("insight.diagnosis", "growth")]["conditional"] is True
    assert rows[("signal.anomaly", "analyst")]["conditional"] is False
    assert all(r["reason"] for r in routing.describe()), "每条订阅都要写明理由"


def test_describe_gates_is_separate_from_subscriptions():
    """闸与订阅条件必须分开渲染。

    摆在同一张表里会让人以为它们是同一种规则,而这两者连失败方向都是相反的:
    订阅 fail-closed(判不清就不唤醒),闸 fail-open(判不清就别拦已经该做的事)。
    """
    gates = routing.describe_gates()
    assert {g["target"] for g in gates} == set(routing.GATES)
    assert all(g["reason"] for g in gates), "闸也要能说清它拦的是什么"


def test_priority_labels_cover_every_declared_tier():
    """档位新增而标签没跟上时,管理端会露出裸数字。"""
    for tier in (routing.P_URGENT, routing.P_NORMAL, routing.P_LOW):
        assert tier in routing.PRIORITY_LABELS


# ---------- 链清单聚合 ----------

@pytest.fixture()
def db(tmp_path):
    from app.db.database import Database

    d = Database(db_path=str(tmp_path / "t.db"))
    d.init_schema()
    return d


def test_list_event_chains_groups_by_correlation(db):
    db.publish_event("signal.anomaly", {"kind": "x"}, "service", "analyst", "C1")
    db.publish_event("insight.diagnosis", {}, "analyst", "growth", "C1")
    db.publish_event("signal.anomaly", {"kind": "y"}, "service", "analyst", "C2")

    chains = {c["correlation_id"]: c for c in db.list_event_chains()}
    assert chains["C1"]["events"] == 2
    assert chains["C2"]["events"] == 1
    # 链上碰过的 Agent 去重但**保留首次出现顺序**——顺序本身是给人看的信息
    # (谁先动手),排序会把它打乱。
    assert chains["C1"]["agents"] == ["service", "analyst", "growth"]


def test_list_event_chains_excludes_events_without_correlation(db):
    """没有 correlation_id 的事件不能被并成一条名为空串的"链"。

    那会造出一条把互不相关的事件混在一起的假时间线,比不显示更糟。
    """
    db.publish_event("signal.anomaly", {}, "service", "analyst", "")
    db.publish_event("signal.anomaly", {}, "service", "analyst", "C9")

    ids = {c["correlation_id"] for c in db.list_event_chains()}
    assert ids == {"C9"}


def test_list_event_chains_counts_terminal_states_separately(db):
    """skipped 与 failed 必须分开计数。

    skipped 是消费闸拦下的**正常**结果(营销静默期就走这条),把它并进失败数
    会让运营去修一个根本不存在的故障。
    """
    db.publish_event("a", {}, "s", "growth", "C1")
    db.publish_event("b", {}, "s", "growth", "C1")
    claimed = db.claim_events("growth", limit=2)
    db.finish_event(claimed[0]["id"], status="skipped")
    db.finish_event(claimed[1]["id"], status="failed")

    chain = db.list_event_chains()[0]
    assert chain["skipped"] == 1
    assert chain["failed"] == 1


def test_list_event_chains_orders_by_most_recent(db):
    db.publish_event("a", {}, "s", "analyst", "OLD")
    db.publish_event("b", {}, "s", "analyst", "NEW")
    assert db.list_event_chains()[0]["correlation_id"] == "NEW"


# ---------- 端点接线 ----------

def test_health_endpoint_surfaces_budget_with_process_scope():
    """预算必须带进程作用域说明。

    它读的是**本进程**的计数器,API 进程查到的恒为 0——真正花钱的是 worker。
    不下发等于这道闸在界面上不存在;下发但不说明进程边界,运维会看着 spent=0
    得出"worker 没花过钱"的错误结论。所以 scope/note 是响应的一部分,不是注释。
    """
    import inspect

    from app.api import app as app_module

    src = inspect.getsource(app_module)
    assert "collab.budget_status()" in src, "budget_status 必须真的被端点调用"
    assert '"scope"' in src and '"note"' in src, \
        "预算下发必须附带进程作用域说明,否则 spent=0 会被读反"


def test_routing_endpoint_does_not_duplicate_agent_labels_in_frontend():
    """Agent 中文标签由后端下发,前端不另抄一份。

    多一份副本就多一处会漂移的口径(与 OPPORTUNITY_KINDS 同理)。
    """
    from pathlib import Path

    api = Path(__file__).resolve().parent.parent / "webui" / "src" / "lib" / "api.ts"
    if not api.exists():                      # 纯后端环境跳过
        pytest.skip("webui 不在此环境")
    text = api.read_text(encoding="utf-8")
    assert "RoutingAgent" in text, "前端应消费后端下发的名册类型"
    assert "参谋 Agent" not in text, "中文标签不该在前端硬编码第二份"


def test_bus_agent_constants_are_the_only_source_of_agent_keys():
    """端点里出现的 agent key 必须来自 bus 常量,不能手写字符串。"""
    import inspect

    from app.api import app as app_module

    src = inspect.getsource(app_module)
    for const in ("bus.AGENT_SERVICE", "bus.AGENT_ANALYST",
                  "bus.AGENT_GROWTH", "bus.AGENT_HUMAN"):
        assert const in src, f"{const} 应由常量引用,不该在端点里手写字面量"
    assert bus.AGENT_ANALYST == "analyst"      # 常量本身没被改名
