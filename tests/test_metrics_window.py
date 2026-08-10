"""看板指标的统计时间窗。

改造前只有"全部历史"一种口径。后果在这次走查里当场撞到:MCP 数据源问题修好
之后,新的工具调用全部成功,但看板上的工具成功率仍被几百条旧失败压着——运维
看到的是"改了没用",实际是"口径不对"。**一个已经修好的问题不该在看板上红几周。**
"""

from __future__ import annotations

import time

import pytest

from app.observability.metrics import compute_metrics
from app.observability.store import TraceStore
from app.observability.trace import Span, Trace


def _trace(tid: str, started_at: float, tool_ok: bool) -> Trace:
    tr = Trace(trace_id=tid, session_id="s", user_input="hi", intent=None,
               started_at=started_at, ended_at=started_at + 0.1, latency_ms=100.0,
               status="ok", error=None)
    tr.spans.append(Span(span_id=f"{tid}-sp", trace_id=tid, name="tool:query_order",
                         kind="tool", started_at=started_at, ended_at=started_at,
                         latency_ms=1.0, success=tool_ok))
    return tr


@pytest.fixture()
def store(tmp_path):
    s = TraceStore(str(tmp_path / "t.db"))
    s.init_schema()
    now = time.time()
    # 旧的三条全失败(模拟修复前的历史),新的两条全成功(修复后)
    for i in range(3):
        s.save_trace(_trace(f"old{i}", now - 86400 * 3, tool_ok=False))
    for i in range(2):
        s.save_trace(_trace(f"new{i}", now - 60, tool_ok=True))
    return s


def test_window_excludes_old_failures(store):
    """短窗口下只看得到新数据——这正是"修好了要能看出来"的意思。"""
    m = compute_metrics(store, window_hours=1)
    assert m["total_traces"] == 2
    assert m["tool_success_rate"] == 1.0


def test_cumulative_is_dragged_down_by_history(store):
    """对照组:全历史口径下仍是 2/5,红色褪不掉。这条钉的是问题本身。"""
    m = compute_metrics(store, window_hours=None)
    assert m["total_traces"] == 5
    assert m["tool_success_rate"] == pytest.approx(2 / 5)


def test_default_signature_behaviour_unchanged(store):
    """`compute_metrics(store)` 的行为必须逐字节不变。

    它有既有回归钉着,不该因为新增一个能力就改掉旧语义。
    """
    assert compute_metrics(store) == compute_metrics(store, window_hours=None)


def test_window_is_reported_with_the_numbers(store):
    """口径要跟着数字一起下发——一个百分比脱离统计窗口就没有意义。"""
    assert compute_metrics(store, window_hours=24)["window_hours"] == 24
    assert compute_metrics(store)["window_hours"] is None


def test_zero_or_negative_window_means_all_history(store):
    """端点用 0 表示"全部",不能被当成"近 0 小时"而返回空。"""
    assert compute_metrics(store, window_hours=0)["total_traces"] == 5
    assert compute_metrics(store, window_hours=-5)["total_traces"] == 5


def test_empty_window_does_not_divide_by_zero(store):
    """窗口内没有任何数据时,各比率取 0 而不是抛 ZeroDivisionError。"""
    m = compute_metrics(store, window_hours=1 / 3600)   # 1 秒
    assert m["total_traces"] == 0
    assert m["error_rate"] == 0.0
    assert m["tool_success_rate"] == 0.0
    assert m["latency_p50_ms"] == 0.0


def test_spans_are_filtered_by_their_own_timestamp(store):
    """span 按自己的 started_at 过滤,不靠先查 trace 再关联。

    后者要么发一条 `IN (几百个 id)`,要么两次查询在 Python 里 join,而这个端点
    是看板每次刷新都要调的。
    """
    assert len(store.all_spans(since=time.time() - 3600)) == 2
    assert len(store.all_spans()) == 5


def test_guard_metrics_still_read_raw_meta_string(store):
    """窗口过滤不能顺手把 all_spans 的 meta 解析掉。

    `compute_metrics` 按字符串包含判护栏动作,解析成 dict 会当场把拦截计数
    改成 0。见 `TraceStore._decode_meta` 的说明。
    """
    rows = store.all_spans(since=time.time() - 3600)
    assert all(isinstance(r["meta"], str) for r in rows)


def test_trace_db_is_isolated_from_the_real_one():
    """测试绝不能往真实观测库里写。

    实测污染:开发机上 `app/sessions/traces.db` 里 481 条 trace 有 175 条(36%)
    是测试造的,其中 158 条是同一句 fixture 文案「查一下订单」、延迟 0~2ms。
    后果是看板上每个数字都掺了假——P50 被一堆 0ms 假 trace 拉到 0,工具成功率
    混进了打桩调用的结果。而这些数字正是判断"线上现在健不健康"的依据。

    隔离由 `tests/conftest.py::_isolate_trace_db` 完成(session 级 autouse)。
    """
    from app.config.settings import settings

    assert "app/sessions/traces.db" not in settings.trace_db_path.replace("\\", "/"), \
        "trace 库仍指向真实路径,测试会污染看板数据"


def test_suite_does_not_depend_on_the_mcp_server_process():
    """套件不许依赖 `127.0.0.1:9123` 这个独立进程是否在跑。

    本机 `.env` 设了 `MCP_ENABLED=true`(字段默认值是 False),于是每个建 Agent 的
    测试都会去连 MCP:在跑就用远程 9 个工具(与 LOCAL_TOOL_DEFINITIONS 不是同
    一套),没跑就等满 30 秒超时再降级。症状是"同一份代码时绿时红、套件耗时在
    86s/100s/305s/615s 之间乱跳",而红的用例与真正的改动毫无关系。

    这条与 `test_trace_db_is_isolated_from_the_real_one` 同一家族:
    **凡是"套件正确性依赖其取值"的 settings 字段,都必须在 conftest 钉死。**
    """
    from app.config.settings import settings

    assert settings.mcp_enabled is False, \
        "conftest 应把 mcp_enabled 钉成 False,否则套件会去连外部 MCP 进程"
