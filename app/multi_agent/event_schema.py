"""事件标准:每类协作事件的 payload 必需字段。

为什么需要它——**这个风险因为路由表的引入而变严重了**。

改造前 `kind` 只被参谋的处理器读到,漏发它最多让一条诊断的措辞变差。现在它还
决定路由(`routing._marketing_worthy` 读 `kind` 与 `degraded`),漏发的后果变成:

    生产方漏发 kind → 谓词判否 → resolve() 返回 [] → publish 返回 None
    → 整条营销链静默消失

没有异常、没有 failed 事件、时间线上什么都没有。**失败形态是"什么都没发生"**,
这是所有失败形态里最难查的一种——你不会去查一件你不知道没发生的事。

设计取舍:

- **只声明必需字段,不引入校验框架。** payload 是自由 dict,套 Pydantic 会逼
  所有发布点改造成模型对象,收益不抵成本;而真正要防的是"字段没了",不是
  "字段类型错了"。
- **校验不通过照常发布**(见 bus.publish)。发布点之一在买家会话的热路径上
  (streaming.py 的转人工埋点),一条埋点的 schema 问题绝不该让买家那一轮失败。
  漏发的后果由 warning 日志 + 下面那条测试兜住。
- **最重要的保障是测试而不是运行时校验**:
  `tests/test_collab_event_schema.py::test_routing_predicates_only_read_declared_fields`
  钉住"路由谓词读到的每个字段都必须在这里声明"。运行时校验只能告诉你"刚才漏了",
  测试能在合进主干之前就拦住"这个谓词依赖了一个没人保证会有的字段"。
"""

from __future__ import annotations

from app.multi_agent.bus import (EV_DRAFTS_READY, EV_INSIGHT_DIAGNOSIS,
                                 EV_OUTREACH_CONVERTED, EV_OUTREACH_NO_CHANGE,
                                 EV_OUTREACH_SENT, EV_SIGNAL_ANOMALY)

#: 事件类型 → 必需字段。只列**下游真的会读**的字段,不做"把 payload 里所有键
#: 都写一遍"的登记——那样每加一个可选字段都要改这里,很快就没人维护了。
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    # kind 同时被参谋处理器与路由谓词读;subject 是共享上下文的主键
    EV_SIGNAL_ANOMALY: frozenset({"kind", "subject"}),
    # kind/degraded 决定要不要唤醒营销;conclusion 会被原文嵌进买家话术
    EV_INSIGHT_DIAGNOSIS: frozenset({"kind", "degraded", "conclusion"}),
    EV_DRAFTS_READY: frozenset({"drafted"}),
    EV_OUTREACH_SENT: frozenset({"draft_id", "user_id"}),
    EV_OUTREACH_CONVERTED: frozenset({"draft_id", "outcome"}),
    EV_OUTREACH_NO_CHANGE: frozenset({"draft_id", "outcome"}),
}


def missing_fields(event_type: str, payload: dict) -> list[str]:
    """返回缺失的必需字段(已排序,便于日志与断言稳定)。

    未登记的事件类型返回 `[]`——与 `routing.resolve` 对未登记类型的口径一致:
    引入一个新事件类型不该先变成一次故障。
    """
    required = REQUIRED_FIELDS.get(event_type)
    if not required:
        return []
    present = set(payload or {})
    return sorted(required - present)
