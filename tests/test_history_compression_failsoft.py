"""历史压缩失败不能带走一个已经完成的回合。

**实测缺陷**(走查长会话压缩时抓到)。这段代码在整轮走查里**一次都没被触发过**,
而且**零测试覆盖**——全仓没有任何测试提到 `summarize` 或 `compress`。

`app/agent/summarizer.py:summarize` 是一个裸 LLM 调用,没有任何错误处理。而
`_compress_history` 在 `chat()` 里的调用点是:

    _compress_history()      ← 抛异常就到此为止
    _status = "complete"
    store.save(...)          ← 这一轮根本没落盘
    _write_snapshot()
    return result            ← 买家丢掉已经生成好的回复

实测复现两种失败:

    摘要调用超时          → TimeoutError 一路冒出 chat()
    模型返回 content=None → AttributeError: 'NoneType' object has no attribute 'strip'

后者尤其阴:部分模型/网关在被截断或触发内容过滤时就是返回 None,不是"坏运气"。

**为什么不能"失败就什么都不做"**:历史会继续涨,下一轮更可能撞上真正的上下文上限,
于是这一次的软失败会变成下一次的硬失败。所以退到 `fallback_summary`——不调模型的
确定性节录,保留**尾部**内容(越近的对下一轮越有用),订单号/金额这类短字符串能活下来。

顺带说清一个**没有**问题的地方:split 的回退循环
(`while split > 0 and raw_messages[split].get("role") in ("tool",)`)是对的——
它保证 `recent` 不会以孤立的 tool 消息开头(那种消息的 assistant tool_calls 已经被压走,
发给模型会直接报错)。
"""

from types import SimpleNamespace

import pytest

from app.agent.chat import EcomAgent
from app.agent.summarizer import (FALLBACK_SUMMARY_MAX_CHARS, fallback_summary,
                                  summarize)


def _history(n=10):
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"第{i}轮:我的订单 ORD-20240115-001 到哪了"})
        msgs.append({"role": "assistant", "content": f"第{i}轮回复,金额 ¥899.00"})
    return msgs


def _client(behaviour):
    class C:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return behaviour()
    return C()


def _boom():
    raise TimeoutError("LLM 超时")


def _none():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None))])


def _blank():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="   "))])


def _ok():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="摘要:买家在问 ORD-20240115-001"))])


def _agent(client, msgs=None, keep=3):
    events = []
    fake = SimpleNamespace(history_keep_recent=keep, raw_messages=list(msgs or _history()),
                           summary=None, client=client, model="m",
                           _emit=lambda e: events.append(e))
    return fake, events


# --------------------------------------------------------------------------
# 核心:三种失败都不能抛
# --------------------------------------------------------------------------

@pytest.mark.parametrize("behaviour,exc_name", [
    (_boom, "TimeoutError"),
    (_none, "ValueError"),      # summarize 把空内容转成 ValueError,不再 AttributeError
    (_blank, "ValueError"),
])
def test_compression_failure_does_not_raise(behaviour, exc_name):
    """修复前:异常一路冒出 chat(),买家丢掉已生成的回复且这一轮没落盘。"""
    fake, events = _agent(_client(behaviour))
    EcomAgent._compress_history(fake)          # 不抛就是通过

    assert len(fake.raw_messages) == 3, "上下文没有被压下去"
    assert fake.summary, "既没摘要也没节录,历史等于白丢了"
    assert [(e["kind"], e["hits"]) for e in events] == [("summary_degraded", [exc_name])]


def test_none_content_no_longer_attribute_errors():
    """`content=None` 要在 summarize 里转成明确的 ValueError,而不是 `.strip()` 崩。

    部分模型/网关在被截断或触发内容过滤时就是返回 None——这不是坏运气。
    """
    with pytest.raises(ValueError, match="空内容"):
        summarize(client=_client(_none), model="m", old_messages=_history(2), prev_summary=None)


def test_key_facts_survive_the_fallback():
    """兜底节录必须留住订单号这类关键事实,否则"压缩成功"等于"忘了刚才在说什么"。"""
    fake, _ = _agent(_client(_boom))
    EcomAgent._compress_history(fake)
    assert "ORD-20240115-001" in fake.summary
    assert "非摘要" in fake.summary, "要说清这是节录而不是摘要,别让下游把它当成结论"


def test_normal_path_unchanged():
    fake, events = _agent(_client(_ok))
    EcomAgent._compress_history(fake)
    assert fake.summary == "摘要:买家在问 ORD-20240115-001"
    assert events == [], "正常路径不该发降级事件"
    assert len(fake.raw_messages) == 3


# --------------------------------------------------------------------------
# 兜底节录本身
# --------------------------------------------------------------------------

def test_fallback_keeps_the_tail_not_the_head():
    """超长时保留**尾部**:越近的内容对下一轮越有用。"""
    # 必须真的超过 FALLBACK_SUMMARY_MAX_CHARS,否则测不到截断分支(第一版只造了 800 字)
    msgs = [{"role": "user", "content": "最早的话" * (FALLBACK_SUMMARY_MAX_CHARS // 2)},
            {"role": "user", "content": "最近说的是 ORD-9999"}]
    out = fallback_summary(msgs)
    assert "ORD-9999" in out
    assert "省略" in out


def test_fallback_is_bounded():
    msgs = [{"role": "user", "content": "很长的话" * 2000}]
    out = fallback_summary(msgs)
    assert len(out) < FALLBACK_SUMMARY_MAX_CHARS + 200


def test_fallback_carries_previous_summary():
    """连续两次压缩都失败时,上一次的摘要不能被丢掉。"""
    out = fallback_summary([{"role": "user", "content": "新话"}], prev_summary="上次的摘要")
    assert "上次的摘要" in out and "新话" in out


def test_fallback_handles_tool_messages():
    msgs = [{"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "query_order", "arguments": "{}"}}]},
            {"role": "tool", "content": '{"success": true, "order_id": "ORD-1"}'}]
    out = fallback_summary(msgs)
    assert "query_order" in out and "ORD-1" in out


def test_fallback_never_raises_on_odd_input():
    """兜底路径必须可预测、不会自己再失败一次。"""
    assert fallback_summary([]) is not None
    assert fallback_summary([{"role": "user"}]) is not None            # 缺 content
    assert fallback_summary([{"content": "x"}]) is not None            # 缺 role
    assert fallback_summary([{"role": "assistant", "tool_calls": None}]) is not None


# --------------------------------------------------------------------------
# split 回退:不能让 recent 以孤立的 tool 消息开头(既有行为,顺手钉住)
# --------------------------------------------------------------------------

def test_recent_never_starts_with_orphan_tool_message():
    """孤立的 tool 消息发给模型会直接报错(它的 assistant tool_calls 已被压走)。"""
    msgs = [{"role": "user", "content": "查一下"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "query_order", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
            {"role": "assistant", "content": "查到了"}]
    fake, _ = _agent(_client(_ok), msgs=msgs, keep=2)
    EcomAgent._compress_history(fake)
    assert fake.raw_messages[0].get("role") != "tool", "recent 以孤立 tool 消息开头"


def test_no_compression_when_history_is_short():
    fake, events = _agent(_client(_boom), msgs=_history(1), keep=3)
    before = list(fake.raw_messages)
    EcomAgent._compress_history(fake)
    assert fake.raw_messages == before and fake.summary is None
    assert events == []
