# 记忆更新节流(STM 每 N 轮异步 + 长会话中途抽取) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** ①短期记忆更新从"每轮同步 LLM 调用"(实测 ~15s/轮、533+679 tok)改为**每 N 轮 + 异步后台**——响应不再被阻塞,token 降为 1/N;②长会话**中途每 M 轮**触发一次隐式记忆抽取归档(与会话末巩固双通道,策展去重兜底),超长会话的行为模式不再迟到。

**Architecture:** 节流与异步全部收在 `MemoryManager` 层(chat.py 只多传一个 `all_messages` 参数)——轮次计数器 + 单把后台锁串行化一切后台记忆任务(STM/中途抽取/会话末巩固互斥,防并发写 LTM);STM 的 `facts` 是整列表原子赋值,后台写主线程读天然安全;后台 LLM 调用包 `background_trace`(Langfuse 里成为命名 trace,不产生游离 generation)。中途抽取用**消息游标**切片(只喂上次抽取之后的新消息),会话末巩固复用同一游标只抽尾段——两通道不重复烧 token。

**Tech Stack:** threading(标准库)、现有 MemoryManager/LongTermMemory/背景 trace。零新依赖。

## Global Constraints

- **节流/异步逻辑只进 `app/agent/memory/manager.py`**;`app/agent/chat.py` 仅把调用改为 `update_short_term(self.raw_messages[-6:], all_messages=self.raw_messages)`(一行);不动 `short_term.py`/`extraction.py`/`long_term.py`。
- **settings(逐字)**:`stm_update_every_n_turns: int = 3`(1=每轮,等价旧行为)、`memory_checkpoint_every_n_turns: int = 10`(0=关闭中途抽取)、`memory_async_updates: bool = True`(False=同步执行,测试/调试用)。
- **conftest 强制 `memory_async_updates = False`**(既有测试确定性;异步专项测试自行开启),写法照搬 auth/langfuse 强制关。
- **并发安全铁律**:`MemoryManager` 持一把 `threading.Lock`(`_bg_lock`),STM 后台更新、中途抽取、`consolidate_to_long_term` 三者全部 `with self._bg_lock` 串行;传给后台线程的消息列表必须**先浅拷贝快照**(主线程会继续 append);后台任何异常吞掉(best-effort,绝不影响对话)。
- **游标语义**:`_extract_cursor`(int,初值 0)记录"已抽取到的 raw_messages 下标";中途抽取喂 `all_messages[cursor:]` 并推进游标;`consolidate_to_long_term` 同样从游标起切片、完成后推进——**直接调用(不经节流路径)时游标为 0 = 全量,向后兼容**。会话恢复(重启)后游标归 0,首次触发会重抽旧消息——策展/去重兜底,注释说明为已知取舍。
- **Langfuse**:后台 STM 更新包 `background_trace("update_short_term", session_id=..., user_id=...)`,中途抽取包 `background_trace("memory_checkpoint", ...)`;session_id 在**主线程**用 `bargain.get_current_session()` 捕获后传入线程(ContextVar 不跨线程)。
- **memory_enabled=False 时一切照旧短路**;计数器不因短路轮次错乱(短路时不计数)。
- 每任务:独立提交 + 指定测试文件绿(**切勿全量 pytest**);commit 末行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;推送 origin,绝不 upstream;`.venv/Scripts/python.exe`;测试不 print emoji。

## File Structure

| 文件 | 任务 | 责任 |
|---|---|---|
| `app/config/settings.py`(改) | N1 | 三个字段 |
| `app/agent/memory/manager.py`(改) | N1/N2 | 计数器/节流/异步/锁/游标/中途抽取 |
| `app/agent/chat.py`(改一行) | N1 | 传 `all_messages` |
| `tests/conftest.py`(改) | N1 | 强制同步 |
| `tests/test_memory_throttle.py`(新) | N1/N2 | 全部行为测试 |

---

### Task N1: STM 每 N 轮 + 异步后台

**Files:**
- Modify: `app/config/settings.py`(Memory 分区)、`app/agent/memory/manager.py`、`app/agent/chat.py:133`、`tests/conftest.py`
- Test: `tests/test_memory_throttle.py`(新)

**Interfaces:**
- Produces(N2 依赖):`MemoryManager.update_short_term(recent_messages: list, all_messages: list | None = None) -> None`(节流入口,N2 在同一入口挂中途抽取);`self._turn_count: int`、`self._bg_lock: threading.Lock`、`self._bg_thread: threading.Thread | None`(测试 join 用);内部 `_run_bg(fn)`(按 `memory_async_updates` 同步或起 daemon 线程执行)。

- [ ] **Step 1: 写失败测试** `tests/test_memory_throttle.py`

```python
"""记忆更新节流:STM 每 N 轮 + 异步;中途抽取游标。全离线(fake client)。"""

import threading

from app.agent.memory.manager import MemoryManager
from app.config.settings import settings


class FakeClient:
    """脚本化 chat.completions.create;记录调用并返回固定事实 JSON。"""

    def __init__(self, reply='["用户偏好红色"]'):
        self.calls = []
        self.reply = reply
        outer = self

        class _C:
            def create(self, **kw):
                outer.calls.append(kw)
                class _Msg:  # noqa: N801
                    content = outer.reply
                class _Choice:
                    message = _Msg()
                class _Resp:
                    choices = [_Choice()]
                    usage = None
                return _Resp()
        class _Chat:
            completions = _C()
        self.chat = _Chat()


def _mgr(tmp_path, **kw):
    return MemoryManager(client=FakeClient(), model="m", user_id="u1",
                         memory_dir=str(tmp_path / "mem"), memory_enabled=True, **kw)


MSGS = [{"role": "user", "content": "我喜欢红色"}]


def test_stm_updates_only_every_n_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 3)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    m = _mgr(tmp_path)
    for _ in range(6):
        m.update_short_term(MSGS, all_messages=MSGS)
    assert len(m.client.calls) == 2          # 第 3、6 轮各 1 次


def test_n_equals_1_keeps_legacy_every_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 1)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    m = _mgr(tmp_path)
    for _ in range(3):
        m.update_short_term(MSGS, all_messages=MSGS)
    assert len(m.client.calls) == 3


def test_disabled_memory_short_circuits_without_counting(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 3)
    m = _mgr(tmp_path, memory_enabled=False)
    for _ in range(6):
        m.update_short_term(MSGS, all_messages=MSGS)
    assert m.client.calls == [] and m._turn_count == 0


def test_async_mode_runs_in_background_and_updates_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 1)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    monkeypatch.setattr(settings, "memory_async_updates", True)
    m = _mgr(tmp_path)
    m.update_short_term(MSGS, all_messages=MSGS)
    assert m._bg_thread is not None
    m._bg_thread.join(timeout=5)
    assert m.stm.facts == ["用户偏好红色"]
    # 后台线程不是主线程
    assert m._bg_thread is not threading.current_thread()


def test_async_snapshot_isolated_from_caller_mutation(tmp_path, monkeypatch):
    """传入列表在派发后被主线程继续 append,后台看到的是快照。"""
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 1)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    monkeypatch.setattr(settings, "memory_async_updates", True)
    m = _mgr(tmp_path)
    msgs = [{"role": "user", "content": "A"}]
    m.update_short_term(msgs, all_messages=msgs)
    msgs.append({"role": "user", "content": "B"})     # 派发后突变
    m._bg_thread.join(timeout=5)
    sent = m.client.calls[0]
    assert "B" not in str(sent)                        # 快照不含突变


def test_bg_exception_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 1)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    monkeypatch.setattr(settings, "memory_async_updates", True)
    m = _mgr(tmp_path)
    def boom(*a, **k):
        raise RuntimeError("boom")
    m.stm.update = boom
    m.update_short_term(MSGS, all_messages=MSGS)       # 不抛
    m._bg_thread.join(timeout=5)                        # 线程正常结束
```

- [ ] **Step 2: 跑失败** → `.venv/Scripts/python.exe -m pytest tests/test_memory_throttle.py -q` FAIL

- [ ] **Step 3: settings**(Memory 分区末尾):

```python
    stm_update_every_n_turns: int = 3     # 短期记忆每 N 轮更新一次(1=每轮,旧行为);省 token 且中间轮原始消息本就在上下文
    memory_checkpoint_every_n_turns: int = 10   # 长会话中途每 N 轮触发隐式记忆抽取归档(0=关);与会话末巩固双通道,策展去重
    memory_async_updates: bool = True     # 记忆更新走后台线程,不阻塞回复(False=同步,测试/调试)
```

conftest(照搬 auth 强制关的三行结构):`_orig_async = settings.memory_async_updates` / `settings.memory_async_updates = False` / 恢复。

- [ ] **Step 4: manager.py 实现**

`__init__` 增:

```python
        import threading
        self._turn_count = 0
        self._extract_cursor = 0            # N2:已抽取到的消息游标
        self._bg_lock = threading.Lock()    # 串行化后台记忆任务(STM/中途抽取/会话末巩固)
        self._bg_thread = None
```

替换 `update_short_term`:

```python
    def update_short_term(self, recent_messages: list[dict],
                          all_messages: list[dict] | None = None) -> None:
        """每轮对话后调用:按 stm_update_every_n_turns 节流,按需异步执行。

        节流依据:中间轮次的原始消息本来就在上下文里,摘要不需要每轮刷新;
        实测每轮同步更新一次 LLM 调用 ~15s/500+ tok,是响应时间大头。
        """
        if not self.memory_enabled:
            return
        from app.config.settings import settings as _s
        self._turn_count += 1
        n = max(1, _s.stm_update_every_n_turns)
        due_stm = self._turn_count % n == 0
        m = _s.memory_checkpoint_every_n_turns
        due_ckpt = m > 0 and self._turn_count % m == 0 and all_messages
        if not due_stm and not due_ckpt:
            return

        recent = list(recent_messages)                       # 快照:主线程会继续 append
        full = list(all_messages) if all_messages else []
        try:
            from app.agent.tools.bargain import get_current_session
            session_id = get_current_session() or ""
        except Exception:
            session_id = ""

        def work():
            from app.observability.langfuse_bridge import background_trace
            with self._bg_lock:
                if due_stm:
                    try:
                        with background_trace("update_short_term",
                                              session_id=session_id, user_id=self.ltm.user_id):
                            self.stm.update(self.client, self.model, recent)
                    except Exception:
                        pass
                if due_ckpt:
                    self._checkpoint_extract(full, session_id)

        self._run_bg(work)

    def _run_bg(self, fn) -> None:
        from app.config.settings import settings as _s
        if not _s.memory_async_updates:
            fn()
            return
        import threading
        t = threading.Thread(target=fn, daemon=True, name="memory-bg")
        self._bg_thread = t
        t.start()
```

`_checkpoint_extract` 本任务先放**空实现**(N2 填):

```python
    def _checkpoint_extract(self, all_messages: list[dict], session_id: str) -> None:
        """长会话中途隐式记忆抽取(N2 实现;N1 占位无操作)。"""
        return
```

`consolidate_to_long_term` 包锁(与后台互斥;逻辑不变):

```python
    def consolidate_to_long_term(self, messages, summary) -> None:
        if not self.memory_enabled:
            return
        with self._bg_lock:
            self.ltm.extract_and_save(self.client, self.model,
                                      messages[self._extract_cursor:], summary)
            self._extract_cursor = len(messages)
```

chat.py:133 改为:

```python
        self.memory_manager.update_short_term(self.raw_messages[-6:],
                                              all_messages=self.raw_messages)
```

- [ ] **Step 5: 跑通过 + 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_throttle.py tests/test_memory_write_tool.py tests/test_memory_tool_isolation.py tests/test_react_degrade.py tests/test_chat_pipeline_wiring.py tests/test_consolidate_api.py tests/test_session_reaper.py -q`
Expected: PASS(conftest 已强制同步;若既有测试直接调 `update_short_term(msgs)` 单参数——签名向后兼容,且 N 默认 3 会改变其节奏:**受影响的既有断言按"每 3 轮"语义同步或在该测试内 monkeypatch N=1**,报告说明)

- [ ] **Step 6: 提交** `feat(memory): N1 STM 更新每 N 轮 + 异步后台(不阻塞回复)`

---

### Task N2: 长会话中途抽取归档(游标切片)

**Files:**
- Modify: `app/agent/memory/manager.py`(填 `_checkpoint_extract`)
- Test: `tests/test_memory_throttle.py`(增)

**Interfaces:**
- Consumes: N1 的游标/锁/节流入口(due_ckpt 已算好)。
- Produces: 中途抽取行为——`ltm.extract_and_save(client, model, all_messages[cursor:], None)` + 游标推进;与会话末巩固共享游标,两通道不重复抽取。

- [ ] **Step 1: 写失败测试**(增到 `tests/test_memory_throttle.py`;FakeClient 需按调用序返回:抽取调用返回 `{"facts": [...], "summary": null}` 形态——**先读 `app/agent/memory/extraction.py` 确认 extract_long_term_facts 期望的输出格式**,按真实格式写脚本;下面断言以行为为准)

```python
def test_checkpoint_extracts_at_every_m_turns_with_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 100)   # 静音 STM,只看抽取
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 2)
    m = _mgr(tmp_path)
    all_msgs = []
    for i in range(4):
        all_msgs.append({"role": "user", "content": f"第{i}句"})
        m.update_short_term(MSGS, all_messages=all_msgs)
    # 第 2、4 轮各触发一次抽取
    assert len(m.client.calls) == 2
    # 第二次抽取只喂游标之后的新消息(不含"第0句")
    assert "第0句" not in str(m.client.calls[1])
    assert "第2句" in str(m.client.calls[1]) or "第3句" in str(m.client.calls[1])
    assert m._extract_cursor == len(all_msgs)


def test_checkpoint_disabled_when_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 100)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 0)
    m = _mgr(tmp_path)
    for i in range(4):
        m.update_short_term(MSGS, all_messages=[{"role": "user", "content": "x"}] * (i + 1))
    assert m.client.calls == []


def test_final_consolidate_only_tail_after_checkpoint(tmp_path, monkeypatch):
    """会话末巩固只抽游标之后的尾段——两通道不重复烧 token。"""
    monkeypatch.setattr(settings, "stm_update_every_n_turns", 100)
    monkeypatch.setattr(settings, "memory_checkpoint_every_n_turns", 2)
    m = _mgr(tmp_path)
    all_msgs = [{"role": "user", "content": "早期消息"}, {"role": "user", "content": "第二句"}]
    m.update_short_term(MSGS, all_messages=all_msgs)
    m.update_short_term(MSGS, all_messages=all_msgs)   # 第 2 轮触发抽取,游标=2
    calls_before = len(m.client.calls)
    all_msgs.append({"role": "user", "content": "尾段新消息"})
    m.consolidate_to_long_term(all_msgs, None)
    tail_call = str(m.client.calls[-1])
    assert "尾段新消息" in tail_call and "早期消息" not in tail_call
    assert len(m.client.calls) == calls_before + 1
```

- [ ] **Step 2: 跑失败**

- [ ] **Step 3: 实现 `_checkpoint_extract`**

```python
    def _checkpoint_extract(self, all_messages: list[dict], session_id: str) -> None:
        """长会话中途隐式记忆抽取:只喂游标之后的新消息,完成后推进游标。

        与会话末巩固双通道共享游标——互不重复抽取;重启后游标归 0 会重抽
        旧消息,由策展/内容去重兜底(已知取舍)。调用方已持 _bg_lock。
        """
        segment = all_messages[self._extract_cursor:]
        if not segment:
            return
        try:
            from app.observability.langfuse_bridge import background_trace
            with background_trace("memory_checkpoint",
                                  session_id=session_id, user_id=self.ltm.user_id,
                                  input={"segment_len": len(segment)}):
                self.ltm.extract_and_save(self.client, self.model, segment, None)
            self._extract_cursor = len(all_messages)
        except Exception:
            pass
```

(注意:`consolidate_to_long_term` 在 N1 已包锁并用游标切片;`_checkpoint_extract` 由 `work()` 在锁内调用,**不得再拿锁**。)

- [ ] **Step 4: 跑通过** → `pytest tests/test_memory_throttle.py tests/test_memory_curation.py tests/test_consolidate_api.py tests/test_session_reaper.py -q` PASS
- [ ] **Step 5: 提交** `feat(memory): N2 长会话中途记忆抽取归档(游标切片,双通道不重复)`

---

### Task N3: 端到端冒烟(控制方执行)

- [ ] 重启服务(默认 N=3/M=10/异步开)→ 登录用户连聊 3 轮
- [ ] 响应体感:回复到达后无 15s 尾巴(异步化)——对比 Langfuse 该轮 trace:根 trace 总时长明显小于此前(记忆更新不在 invoke_agent 内)
- [ ] Langfuse 出现独立的 `update_short_term` trace(第 3 轮后,带 session/user)
- [ ] `.env` 临时设 `MEMORY_CHECKPOINT_EVERY_N_TURNS=3` 重启,连聊 3 轮 → 出现 `memory_checkpoint` trace,LTM 文件新增隐式事实(不点巩固)
- [ ] 恢复 .env,`.superpowers/sdd/progress.md` 记账

## 总量与顺序

N1(~0.4d)→ N2(~0.3d)→ N3(~0.1d),共 **~0.8 人日**。

## Self-Review

- **覆盖核对**:①每 N 轮节流(N1 测试 1/2)+异步不阻塞(测试 4)✅;②中途抽取每 M 轮+游标切片+0=关(N2 测试三条)✅;双通道不重复(final_consolidate_only_tail)✅;Langfuse background_trace 两处 ✅;并发安全(单锁串行+快照隔离测试)✅;memory_enabled 短路不计数 ✅;conftest 强制同步 ✅。
- **占位符扫描**:N2 Step1 标注"先读 extraction.py 确认输出格式再写 FakeClient 脚本"——给了具体动作与断言基准,非 TBD;`_checkpoint_extract` 在 N1 是显式声明的占位、N2 填充,两任务接口一致。
- **类型一致性**:`update_short_term(recent_messages, all_messages=None)` N1 定义/chat.py 调用/N2 测试一致;`_extract_cursor/_bg_lock/_bg_thread` 命名贯穿;`background_trace(name, session_id, user_id, input)` 与现有签名一致。
- **已知取舍**:重启游标归 0(策展兜底);`_bg_thread` 只存最近一个(测试 join 足够,生产 daemon 自生自灭);STM 与 checkpoint 同一线程顺序执行(简单,避免双线程)。
