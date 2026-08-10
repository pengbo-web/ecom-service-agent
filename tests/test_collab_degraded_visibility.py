"""归因降级必须可见。

**这条是把协作 worker 真正跑起来才撞到的。** 跑一轮 `--once`,worker 报告:

    analyst={'claimed': 20, 'done': 20, 'failed': 0}

一片绿色。但日志里 20 条全是「参谋归因 LLM 调用失败,降级为纯统计」——
归因一次都没成功。**一次完全无效的运行和一次健康的运行在界面上长得一模一样。**

更隐蔽的是:降级的诊断按路由规则不唤醒营销(`_marketing_worthy` 判 degraded 直接
返回 False),而 `resolve()` 返回空目标时 `publish()` 压根不插入事件行——于是降级
诊断在总线上**不留一丝痕迹**。协作链上只看得到 signal.anomaly 变 done,然后
什么都没有,链就这么断了。
"""

from __future__ import annotations

import inspect
import json

from app.api import app as app_module
from app.multi_agent.collab import _degraded_conclusion


def _stats_source() -> str:
    src = inspect.getsource(app_module)
    start = src.index("def _collab_degraded_stats")
    end = src.index('/api/admin/collab/chains', start)
    return src[start:end]


# ---------- 降级文案 ----------

def test_signal_kind_without_metrics_does_not_render_none():
    """信号型异常没有 value/threshold,不能拼出「为 None，已超过告警线 None」。

    实测写进共享上下文、店主在参谋面板上直接读到的就是那句话。两个 None 不只是
    难看:它让人以为系统读到了一个空指标,而真相是这类异常本来就没有指标。
    """
    text = _degraded_conclusion({"kind": "service_escalation"}, "s1")
    assert "None" not in text
    assert "service_escalation" in text
    assert "告警线" not in text, "没有阈值就不该提告警线"


def test_metric_kind_still_states_value_and_threshold():
    """指标型异常仍要给出数值与阈值——修的是"缺值时别硬拼",不是"一律不说"。"""
    text = _degraded_conclusion(
        {"kind": "refund_rate_high", "subject_name": "Nike 跑鞋",
         "value": 0.31, "threshold": 0.2}, "P001")
    assert "0.31" in text and "0.2" in text
    assert "Nike 跑鞋" in text


def test_degraded_conclusion_always_says_it_is_degraded():
    """两种文案都必须写明"归因暂不可用"——否则店主会把纯统计当成归因结论。"""
    for anomaly in ({"kind": "service_escalation"},
                    {"kind": "refund_rate_high", "value": 1, "threshold": 0}):
        assert "归因暂不可用" in _degraded_conclusion(anomaly, "s")


# ---------- 降级统计的数据源 ----------

def test_stats_read_shared_context_not_events():
    """必须从 shared_context 数,不能从 insight.diagnosis 事件数。

    降级诊断不产生任何总线事件。实测库里 insight.diagnosis 事件只有 2 条,而
    shared_context 里有 6 条诊断、其中 3 条降级——按事件数统计会得出"降级 0 条"
    这个**恰好相反**的结论。
    """
    body = _stats_source()
    assert "list_shared_context" in body
    assert "EV_INSIGHT_DIAGNOSIS" not in body, "按事件统计会恒为 0"


def test_stats_flag_all_degraded_explicitly():
    """全降级是一个明确的故障态,不该靠人比较两个数字才发现。"""
    body = _stats_source()
    assert "all_degraded" in body


def test_stats_explain_the_silent_break():
    """note 要说清"事件仍算成功"与"链会静默断掉"这两件事。

    否则运维看到 rate=1.0 也不知道它意味着什么。
    """
    body = _stats_source()
    assert "不会唤醒营销" in body
    assert "静默" in body


# ---------- 链清单刻意不统计降级 ----------

def test_chain_list_does_not_fake_a_degraded_column():
    """链清单里**不该**有 degraded 列——它必然恒为 0,比不做更糟。

    降级诊断不插事件行,按 `agent_events.payload` 统计只会得到一个永远是 0 的
    假信号,而看板上一个恒为 0 的指标会被读成"系统很健康"。
    """
    from app.db import database

    src = inspect.getsource(database.Database.list_event_chains)
    assert "AS degraded" not in src
    assert "shared_context" in src, "应说明降级统计去哪儿看"
