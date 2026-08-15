"""基础设施故障不该被当成"这份 Skill 需要改进"的证据。

**这个问题是走查 Skill 页面时撞出来的。** 页面显示
`track-order 实战成功率 3%(success:1 · tool_error:37)`——而根因是 MCP 连接被
系统代理打断后 Agent 静默降级读了本地库,skill 的流程文档一个字都没写错。

而自进化第 ③ 步(`synthesize_skills.py`)恰恰是拿 `tool_error` 轨迹交给 LLM
重写对应 skill 的。照单全收的结果是:**基础设施坏了,系统去改一份没写错的
流程文档**,产出的候选还要占审批位、走灰度。
"""

from __future__ import annotations

import pytest

from app.agent.skills.execution_trace import (is_infrastructure_failure,
                                              trace_is_infrastructure_only)


@pytest.mark.parametrize("text", [
    "无法连接 hmdp:ConnectError",
    "订单服务暂时不可用，请稍后再试",
    "hmdp 返回异常(503)",
    "ReadTimeout",
    "Connection timed out",
    "未登录或登录已过期",
])
def test_infrastructure_failures_are_recognised(text):
    assert is_infrastructure_failure(text) is True


@pytest.mark.parametrize("text", [
    "退款前必须先用 query_order 核对该订单",
    "该订单已发货，不支持取消",
    "库存不足",
    "商品已下架",
])
def test_business_failures_are_not_filtered(text):
    """真实的流程/业务失败必须留下——它们正是自改进要学的东西。"""
    assert is_infrastructure_failure(text) is False


def test_none_and_empty_are_not_infrastructure():
    assert is_infrastructure_failure(None) is False
    assert is_infrastructure_failure("") is False


def test_plausible_business_error_from_a_broken_dependency_is_not_caught():
    """**这条是刻意钉住这个判据的边界。**

    实测那次故障里,坏掉的数据源返回的是「未找到订单 ORD-20240115-001,请核实
    订单号」——一句像模像样的业务错误。它在这一层与真实业务结果无法区分,
    这个过滤器**挡不住**。

    这不是缺陷而是边界:能挡住它的是失败原因可见性(trace 记 error)与数据源
    一致性,不是关键词表。把这条写成测试,是为了防止后来的人误以为
    "有了这个过滤器就不用管数据源了"。
    """
    assert is_infrastructure_failure("未找到订单 ORD-20240115-001，请核实订单号") is False


def test_trace_with_only_infra_failures_is_dropped():
    calls = [{"name": "query_order", "ok": False, "error": "无法连接 hmdp:ConnectError"}]
    assert trace_is_infrastructure_only(calls) is True


def test_trace_with_a_real_failure_is_kept():
    """既有连接失败、又有真实流程问题时**保留**。

    要求"全部是基础设施"而不是"存在基础设施":宁可多留一条样本,
    也不要把真实缺陷一起滤掉。
    """
    calls = [
        {"name": "query_order", "ok": False, "error": "无法连接 hmdp:ConnectError"},
        {"name": "apply_refund", "ok": False, "error": "退款前必须先核对订单"},
    ]
    assert trace_is_infrastructure_only(calls) is False


def test_all_success_trace_is_not_dropped():
    """没有失败的轨迹不该被这条规则碰到(它走的是别的分支)。"""
    assert trace_is_infrastructure_only([{"name": "x", "ok": True}]) is False
    assert trace_is_infrastructure_only([]) is False
    assert trace_is_infrastructure_only(None) is False


def test_blocked_calls_are_not_counted_as_failures():
    """守卫拦下的调用不算失败(与 execution_trace 既有口径一致):
    守卫拦住跳步、模型随后补齐并成功,是守卫在起作用,不是故障。"""
    calls = [{"name": "apply_refund", "ok": False, "blocked": True, "error": "需先核对订单"}]
    assert trace_is_infrastructure_only(calls) is False


def test_infrastructure_traces_do_not_reach_the_improver():
    """**这条不变式没变,守它的东西变了。**

    原来这里断言的是"`synthesize_skills` 的源码里出现过 `trace_is_infrastructure_only`"
    ——它在归因分层(`attribution.py`)把这道过滤从 1 类推广到 3 类之后变红了,
    而**行为完全正确**:基础设施故障现在由归因规则 ① 判成 `capability_limit`,
    照样不回流。

    断言源码里有没有某个函数名,守的是实现而不是行为:实现一换就红,行为坏了
    却未必红(把那行改成 `if False and trace_is_infrastructure_only(...)` 它照样绿)。
    改成直接验行为。
    """
    from app.agent.skills.attribution import CATEGORY_CAPABILITY_LIMIT, partition

    trace = {"session_id": "s1", "user_id": "u1", "skill_name": "track-order",
             "outcome": "tool_error",
             "tool_calls": [{"name": "query_order", "ok": False, "args": {},
                             "error": "无法连接 hmdp:ConnectError"}]}
    r = partition([trace], [], db=object())
    assert r["flows_back"] == [], "基础设施故障不该回流去改流程文档"
    assert r["counts"][CATEGORY_CAPABILITY_LIMIT] == 1


def test_synthesis_reports_the_drop_instead_of_filtering_silently():
    """剔除必须**说出来**,而且要说清按什么理由剔的。

    静默过滤会让"为什么这轮没产出改进候选"变成一个查不下去的问题。
    """
    import inspect

    from app.scripts import synthesize_skills

    src = inspect.getsource(synthesize_skills)
    assert "已剔除" in src, "剔除条数要打印出来"
    assert "summarize(" in src, "每一类的条数都要报,不能只报回流了几条"
