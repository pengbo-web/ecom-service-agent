"""记忆巩固必须检查会话锁是否真的抢到了。

全项目 5 处 `session_lock.guard(...)`,此前**只有这一处没检查 yield 出来的值**。
`guard()` 超时时 yield `False` 而不是抛异常(由调用方决定怎么办),所以不检查
就等于"抢不到也照样往下走"。

这里往下走的动作是:快照 `raw_messages` → 蒸馏成长期记忆。抢不到锁意味着有一轮
`/api/chat` 正在改这个 list,快照到的是**撕裂的对话**(只有用户那句没有助手回复、
或工具序列写了一半)。而一条错的长期记忆会跨会话反复影响后续回答,比这次巩固
失败糟得多。
"""

from __future__ import annotations

import inspect

from app.api import app as app_module


def _consolidate_source() -> str:
    src = inspect.getsource(app_module)
    start = src.index("def consolidate(session_id")
    return src[start:start + 2600]


def test_guard_result_is_checked():
    body = _consolidate_source()
    assert "guard(session_id) as got" in body, "必须接住 guard 的返回值"
    assert "if not got" in body, "抢不到锁要显式处理,不能继续快照"


def test_busy_is_reported_distinctly_from_no_facts():
    """忙和"没有可记的事实"都是 count=0,必须靠 busy 区分。

    两者含义完全相反:一个要"再聊几句",一个要"稍后重试"。共用一句文案会把
    后者引到完全错误的方向。
    """
    body = _consolidate_source()
    assert '"busy": True' in body
    assert '"reason"' in body


def test_no_distillation_happens_on_a_torn_snapshot():
    """忙的时候必须**直接返回**,不能落到 consolidate_to_long_term。"""
    body = _consolidate_source()
    busy_at = body.index("if not got")
    distil_at = body.index("consolidate_to_long_term")
    ret_at = body.index("return", busy_at)
    assert ret_at < distil_at, "抢不到锁时必须在蒸馏之前返回"


def test_all_guard_sites_now_check_the_result():
    """回归:不允许再出现"不检查 got"的 guard 调用。

    这类"一半组件做对、另一半漏了"的形态在本项目里反复出现过(会话锁降级、
    list_user_orders 的 success 判定、conftest 漏钉 mcp_enabled),
    所以这里钉的是**全部站点**,而不只是修好的那一处。
    """
    src = inspect.getsource(app_module)
    bad = []
    for line_no, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if ".guard(" in s and s.startswith("with ") and " as " not in s:
            bad.append((line_no, s))
    assert not bad, f"这些 guard 调用没接住结果: {bad}"
