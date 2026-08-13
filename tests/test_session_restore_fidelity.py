"""进程重启后的会话恢复:文件后端必须和 Redis 后端存一样多的东西。

**实测缺陷**(走查会话恢复时抓到)。`FileSessionStore.save` 只往下传三个字段:

    save_session(key, state["messages"], state["summary"],
                 short_term_memory=state["short_term_memory"])

而 `_session_state()` 产出的是七个,`status` / `step_seq` / `pending` **被静默丢在门口**。
`save_session` 的 payload 里也没有它们。实测往返:

    存进去 status='in_flight', step_seq=7, pending={'action':'refund',...}
    读回来 status=None,        step_seq=None, pending=None

而 `EcomAgent.__init__` 读的正是这三个:

    self._status = loaded.get("status", "complete")   # 文件后端永远拿到默认值
    if loaded.get("pending"): ...                     # 永远为空
    if self._status == "in_flight":
        self.raw_messages = sanitize_tool_pairs(...)  # 这条分支从不执行

三个后果:

1. **中断回合的修复分支形同虚设。** 进程在"assistant 已发 tool_calls、tool 结果还没
   回来"这一刻被杀,重启后那条孤立的 tool_calls 会原样发给模型——模型 API 会直接
   拒绝(tool_calls 必须紧跟 tool 消息),**买家整个会话卡死**。
2. **"R3:恢复挂起动作(确认前重启也能续)"在文件后端上不成立**,注释写了但做不到。
3. `_checkpoint()` 每步落盘,存的东西里却没有 checkpoint 需要的字段——**纯付 I/O**。

而 `RedisSessionStore` 存的是整块 JSON,这三个字段一直保真(实测确认)。于是同一份
代码换个后端行为就变——**并且 Redis 连不上时它会降级到文件后端**,偏偏那正是最可能
发生重启的时刻(本机 Redis 跑在 Docker 里,而 Docker 引擎卡死在这个项目里是有记录的
常态)。

顺带确认**没有**问题的地方:

- 落盘是原子的(`.tmp` + `os.replace`),写一半被杀不会毁掉整个会话文件;
- 会话文件损坏时 `load_session` 返回 None、降级为新会话,不抛异常;
- 进程内 `SessionManager.get_or_create` 按 session 缓存 agent,所以**同一进程内**
  `pending` 在内存里活着。丢失只发生在重启 / `sweep()` 淘汰 / 多 worker 落到别的进程
  ——而这三个恰好都是生产形态。
"""

import json
import os
import tempfile

import pytest

from app.agent.history_utils import sanitize_tool_pairs
from app.session.store import FileSessionStore


INTERRUPTED = [
    {"role": "user", "content": "帮我退款"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "c1", "type": "function",
                     "function": {"name": "apply_refund", "arguments": "{}"}}]},
]

FULL_STATE = {
    "version": 1,
    "messages": INTERRUPTED,
    "summary": "摘要文本",
    "short_term_memory": {"facts": ["x"]},
    "status": "in_flight",
    "step_seq": 7,
    "pending": {"action": "refund", "order_id": "ORD-1"},
}


@pytest.fixture()
def path():
    return os.path.join(tempfile.mkdtemp(), "s.json")


# --------------------------------------------------------------------------
# 往返保真
# --------------------------------------------------------------------------

def test_file_store_round_trips_the_whole_state(path):
    """**核心断言。** 修复前这三个字段回读全是 None。"""
    fs = FileSessionStore()
    fs.save(path, FULL_STATE)
    back = fs.load(path)

    assert back["status"] == "in_flight"
    assert back["step_seq"] == 7
    assert back["pending"] == {"action": "refund", "order_id": "ORD-1"}
    assert back["summary"] == "摘要文本"
    assert back["short_term_memory"] == {"facts": ["x"]}


def test_fields_are_actually_on_disk(path):
    """不只是回读对了——磁盘上要真有这几个键,否则换个读取实现又会丢。"""
    FileSessionStore().save(path, FULL_STATE)
    on_disk = json.load(open(path, encoding="utf-8"))
    assert {"status", "step_seq", "pending"} <= set(on_disk)


def test_two_backends_agree(path):
    """文件后端与 Redis 后端必须存下同样的东西。

    同一份代码换个后端行为就变是这条缺陷最难查的部分:开发机上用文件、生产上用
    Redis,或者生产上 Redis 一挂降级到文件——症状只在其中一种配置下出现。
    """
    fakeredis = pytest.importorskip("fakeredis")
    from app.session.store import RedisSessionStore

    fs = FileSessionStore()
    fs.save(path, FULL_STATE)
    rs = RedisSessionStore(client=fakeredis.FakeStrictRedis(decode_responses=True),
                           fallback=fs)
    rs.save("sess-1", FULL_STATE)

    f_back, r_back = fs.load(path), rs.load("sess-1")
    for key in ("status", "step_seq", "pending", "summary", "messages"):
        assert f_back[key] == r_back[key], f"两个后端在 {key} 上不一致"


# --------------------------------------------------------------------------
# 向后兼容:老会话文件没有这三个键
# --------------------------------------------------------------------------

def test_old_session_files_still_load(path):
    """线上已有几百个老会话文件,不能因为加字段就读不出来。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "messages": [{"role": "user", "content": "hi"}],
                   "summary": None}, f, ensure_ascii=False)

    back = FileSessionStore().load(path)
    assert back["messages"] == [{"role": "user", "content": "hi"}]
    assert back["status"] is None and back["step_seq"] is None and back["pending"] is None


def test_load_does_not_invent_a_default_status(path):
    """老文件缺这个键时返回 None,**不在这里替它编 "complete"**。

    在存储层塞默认值会让"老文件没有这个字段"和"这个会话确实是完成态"变得分不开;
    兜底属于调用方的语义(`loaded.get("status", "complete")`),该留在调用方。
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "messages": []}, f)
    assert FileSessionStore().load(path)["status"] is None


# --------------------------------------------------------------------------
# 真正要救的那件事:中断回合留下的孤立 tool_calls
# --------------------------------------------------------------------------

def test_interrupted_turn_is_recoverable_after_restart(path):
    """进程在"tool_calls 已发、tool 结果未回"时被杀,重启后必须能自愈。

    修复前:status 读到 None → 默认 complete → 修复分支不执行 → 带孤立 tool_calls
    的历史原样发给模型 → API 拒绝 → **买家整个会话卡死**。
    """
    fs = FileSessionStore()
    fs.save(path, FULL_STATE)
    back = fs.load(path)

    assert back["status"] == "in_flight", "恢复分支的触发条件没能落盘"
    assert back["messages"][-1].get("tool_calls"), "复现前提没成立"

    fixed = sanitize_tool_pairs(back["messages"])
    assert not (fixed and fixed[-1].get("tool_calls")), "孤立 tool_calls 没被补齐"


def test_pending_action_survives_restart(path):
    """"确认前重启也能续":买家被问"确认要退款吗",此时重启,挂起动作不能丢。"""
    fs = FileSessionStore()
    fs.save(path, {**FULL_STATE, "status": "complete",
                   "pending": {"action": "refund", "order_id": "ORD-9"}})
    assert fs.load(path)["pending"] == {"action": "refund", "order_id": "ORD-9"}


# --------------------------------------------------------------------------
# 既有的好性质,顺手钉住(实测无缺陷)
# --------------------------------------------------------------------------

def test_write_is_atomic(path):
    """`.tmp` + `os.replace`:写一半被杀不会留下半个文件顶替掉好文件。"""
    fs = FileSessionStore()
    fs.save(path, FULL_STATE)
    assert not os.path.exists(path + ".tmp"), "临时文件没清掉"
    json.load(open(path, encoding="utf-8"))          # 能解析就说明是完整写入


def test_corrupt_file_degrades_to_new_session(path):
    """损坏的会话文件降级为新会话,不抛异常(会话历史丢了,但服务不挂)。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"version": 1, "messa')
    assert FileSessionStore().load(path) is None


def test_unrecognized_shape_degrades(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"nothing": "useful"}, f)
    assert FileSessionStore().load(path) is None
