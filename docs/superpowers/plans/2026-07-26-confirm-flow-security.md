# 确认流安全修复 + 高优 P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复架构体检发现的两个 P0(确认重放丢用户身份导致 auth 开启时退款失败;泛化确认词"可以/好的"误绑定重放,确认 A 单却执行 B)与四个高优 P1(重放绕过 ToolManager、限流键在可伪造 session_id、意图三面漂移、reaper 持会话锁跨 LLM 巩固)。

**Architecture:** 核心是收紧"确认→重放"的信任边界:重放前显式设置 user 上下文并走 ToolManager;确认信号必须与挂起动作绑定(泛化词不再单独触发重放,除非紧邻 need_confirm 上一轮或用户提及目标单号)。其余为定点加固:限流键改 user_id、`_normal_flow` 返回覆盖后意图对齐 trace、reaper 快照数据后释放锁再跑 LLM。全部 TDD,新测试须在 `auth_enabled=True` 下验证真实生产路径。

**Tech Stack:** Python 3.11 + pytest;不新增依赖。

## Global Constraints

- Python 一律 `.venv/Scripts/python.exe`;pytest `-q`;Windows gbk 控制台不 print emoji
- 提交信息末尾:`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;不推送;`.env` 严禁提交
- **测试红线**:确认流相关新测试**必须显式 `monkeypatch.setattr(settings, "auth_enabled", True)`**——conftest 默认关 auth,是这批 P0 被掩盖的原因;不在 auth 开启下测就等于没测
- 容错红线:重放路径任何加固不得吞掉真实退款结果;身份设置失败不得静默放行越权动作
- 既有接口零破坏:`RISK_ACTIONS`/`is_allowed`/`consent_scope`/`need_confirm_result` 签名不变;`PendingAction(action, tool_name, args, message)` 结构不变
- 现有真实订单号格式 `ORD-8位日期-3位序号`(硬校验已上线);pending.args 里的 order_id 是模型当时调用的真实参数
- 工作目录:`D:\2026项目\ecom-service-agent`(git 仓库根)

---

### Task 1: P0-1 确认重放设置 user 身份 + 走 ToolManager(P1-③合并)

**Files:**
- Modify: `app/api/streaming.py`(`_replay_flow`,149-152 行附近)
- Test: `tests/test_replay_security.py`(新)

**Interfaces:**
- Consumes: `agent.user_id`;`agent.tool_manager.execute_tool(name, arguments) -> str`(已存在,manager.py:83);`app.agent.runtime_context.set_current_user`;`app.agent.tools.ownership.owned_order`
- Produces: 无新接口;修复后确认退款在 auth 开启时能查到订单并执行

- [ ] **Step 1: 写失败测试**

创建 `tests/test_replay_security.py`:

```python
"""确认重放安全:auth 开启时重放必须带 user 身份且走 ToolManager(P0-1 + P1-③)。"""

import json
from types import SimpleNamespace

from app.api.streaming import run_agent_streaming
from app.agent.pending import PendingAction
from app.config.settings import settings


class FakeToolManager:
    def __init__(self):
        self.calls = []

    def execute_tool(self, name, arguments):
        # 记录调用时的 current_user——重放必须已设置身份
        from app.agent.runtime_context import get_current_user
        self.calls.append({"name": name, "args": arguments, "user": get_current_user()})
        return json.dumps({"success": True, "message": "退款已提交"}, ensure_ascii=False)


class FakeAgent:
    def __init__(self):
        self.raw_messages = []
        self.user_id = "u-real"
        self.tool_manager = FakeToolManager()
        self._pending = PendingAction(action="refund", tool_name="apply_refund",
                                      args={"order_id": "ORD-20240110-003", "reason": "不想要了"},
                                      message="请确认退款")
        self.client = None

    def save(self):
        pass


def _drain(agent, text, confirm=False):
    return list(run_agent_streaming(agent, text, session_id="s1", confirm=confirm))


def test_replay_sets_current_user_and_uses_tool_manager(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)   # 生产路径,必须显式开
    agent = FakeAgent()
    events = _drain(agent, "确认", confirm=True)
    tm = agent.tool_manager
    assert len(tm.calls) == 1                                  # 走了 ToolManager,不是直连 registry
    assert tm.calls[0]["name"] == "apply_refund"
    assert tm.calls[0]["user"] == "u-real"                     # 重放时 current_user 已设置(P0-1)
    reply = [e for e in events if e["type"] == "reply"][0]
    assert "退款" in reply["content"]
    assert agent._pending is None                             # 重放后清挂起
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_replay_security.py -q`
Expected: FAIL(重放直连 `registry.execute_tool` 而非 tool_manager;current_user 为 None)

- [ ] **Step 3: 改 `_replay_flow`**

`app/api/streaming.py` 的 `_replay_flow` 中,把:

```python
        from app.agent.tools.registry import execute_tool as _exec
        from app.agent.tools.bargain import set_current_session
        from app.schemas.response import CustomerServiceResponse, IntentType

        _sink({"type": "tool_call", "name": pending.tool_name, "args": pending.args})
        set_current_session(session_id)   # negotiate_price 需要会话上下文
        with _scope(RISK_ACTIONS):
            result_str = _exec(pending.tool_name, pending.args)
```

替换为:

```python
        from app.agent.tools.bargain import set_current_session
        from app.agent.runtime_context import set_current_user
        from app.schemas.response import CustomerServiceResponse, IntentType

        _sink({"type": "tool_call", "name": pending.tool_name, "args": pending.args})
        set_current_session(session_id)   # negotiate_price 需要会话上下文
        set_current_user(getattr(agent, "user_id", None))   # P0-1:重放在新线程,须设身份否则 owned_order 判空
        # P1-③:走 agent 的 ToolManager(与 ReAct 同路径:MCP 身份透传/结果落盘一致),
        # 无 tool_manager(裸引擎/测试桩缺失)时回退 registry
        _tm = getattr(agent, "tool_manager", None)
        with _scope(RISK_ACTIONS):
            if _tm is not None and hasattr(_tm, "execute_tool"):
                result_str = _tm.execute_tool(pending.tool_name, pending.args)
            else:
                from app.agent.tools.registry import execute_tool as _exec
                result_str = _exec(pending.tool_name, pending.args)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_replay_security.py -q`
Expected: 1 passed

- [ ] **Step 5: 提交**

```bash
git add app/api/streaming.py tests/test_replay_security.py
git commit -m "fix(P0): 确认重放设置user身份+走ToolManager(auth开启时退款不再判空/身份透传一致)"
```

---

### Task 2: P0-2 确认信号与挂起动作绑定(泛化词不再单独触发重放)

**Files:**
- Modify: `app/agent/consent.py`(新增绑定判定函数)
- Modify: `app/api/streaming.py`(pending 取用条件,38 行附近)
- Test: `tests/test_replay_security.py`(追加)

**Interfaces:**
- Consumes: `PendingAction.args`(取 order_id)、`is_confirmation`
- Produces: `confirm_targets_pending(user_input: str, pending, explicit_confirm: bool) -> bool` in consent.py——判定本轮确认是否真的指向该挂起动作

- [ ] **Step 1: 写失败测试**(追加到 tests/test_replay_security.py)

```python
def _agent_with_pending(order="ORD-20240110-003"):
    a = FakeAgent()
    a._pending = PendingAction(action="refund", tool_name="apply_refund",
                               args={"order_id": order, "reason": "x"}, message="请确认退款")
    return a


def test_bare_affirmation_after_unrelated_question_does_not_replay(monkeypatch):
    """挂起退款后,agent 问了别的,用户'可以'不应重放退款(P0-2 场景A)。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    agent = _agent_with_pending()
    events = _drain(agent, "可以")           # 无 confirm 标志,只是泛化词
    assert agent.tool_manager.calls == []    # 没重放
    # 落到正常流(FakeAgent 无 chat 会怎样——见实现说明:测试用带 chat 的桩)


def test_explicit_confirm_flag_still_replays(monkeypatch):
    """前端显式 confirm=True 仍然重放(点击确认按钮的正规路径)。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    agent = _agent_with_pending()
    _drain(agent, "确认退款", confirm=True)
    assert len(agent.tool_manager.calls) == 1


def test_confirm_mentioning_target_order_replays(monkeypatch):
    """用户确认时提到了挂起单号 → 绑定成立,重放。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    agent = _agent_with_pending("ORD-20240110-003")
    _drain(agent, "确认对 ORD-20240110-003 退款")
    assert len(agent.tool_manager.calls) == 1


def test_confirm_wrong_order_does_not_replay_pending(monkeypatch):
    """挂起A单退款,用户说'确认取消订单B' → 不重放A单(P0-2 场景B)。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    agent = _agent_with_pending("ORD-20240110-003")
    _drain(agent, "确认取消订单 ORD-20240115-001")   # 提到的是别的单
    assert agent.tool_manager.calls == []
```

（实现说明:`test_bare_affirmation_after_unrelated_question_does_not_replay` 落入正常流会调 `agent.chat`;给 FakeAgent 加一个最小 `chat` 桩返回结构化响应即可。在 FakeAgent 里加:
```python
    def chat(self, user_input):
        from app.schemas.response import CustomerServiceResponse, IntentType
        return CustomerServiceResponse(intent=IntentType.OTHER, confidence=0.9,
                                       reply="好的", requires_human=False,
                                       follow_up_question=None)
    def set_turn_understanding(self, qu):
        self._turn_qu = qu
    _turn_qu = None
```
并给测试传 `hitl=None`,`_drain` 已默认。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_replay_security.py -q`
Expected: FAIL(泛化词"可以"当前会触发重放,场景A/B 断言失败)

- [ ] **Step 3: 加绑定判定**

`app/agent/consent.py` 末尾追加:

```python
def confirm_targets_pending(user_input: str, pending, explicit_confirm: bool) -> bool:
    """本轮确认是否真的指向这个挂起动作。绑定信号(任一成立即绑定):
    ① 前端显式 confirm 标志(点了确认按钮,正规路径);
    ② 用户消息提到了挂起动作的目标订单号(强绑定);
    ③ 用户消息含"确认/确定/同意"这类**强确认词**(非'可以/好的'泛化词)且未提到其它订单号。
    仅凭"可以/好的/是的"这类泛化附和,不足以重放不可逆动作。"""
    if pending is None:
        return False
    if explicit_confirm:
        return True
    text = (user_input or "").strip()
    if not text or len(text) > 30:
        return False
    target = str((getattr(pending, "args", {}) or {}).get("order_id") or "").strip()
    if target and target in text:
        return True                       # 提到本挂起单号 → 强绑定
    # 提到了"别的"订单号(ORD- 格式但不是挂起单)→ 明确指向别处,不重放
    import re
    others = [m for m in re.findall(r"ORD-\d{8}-\d{3}", text) if m != target]
    if others:
        return False
    strong = ("确认", "确定", "同意", "就这么办", "退款吧", "成交")
    return any(w in text for w in strong)
```

- [ ] **Step 4: 改 streaming pending 取用**

`app/api/streaming.py` 把:

```python
    confirmed = confirm or is_confirmation(user_input)
    granted = RISK_ACTIONS if confirmed else frozenset()
    pending = getattr(agent, "_pending", None) if confirmed else None
```

替换为:

```python
    from app.agent.consent import confirm_targets_pending
    _raw_pending = getattr(agent, "_pending", None)
    # 重放门:确认信号必须与挂起动作绑定,泛化词"可以/好的"不足以重放不可逆动作(P0-2)
    pending = _raw_pending if confirm_targets_pending(user_input, _raw_pending, confirm) else None
    # 本轮风险授权:显式 confirm,或指向挂起动作的确认(与重放门一致,避免"可以"泛化放行)
    confirmed = pending is not None
    granted = RISK_ACTIONS if confirmed else frozenset()
```

（注意:`is_confirmation` 保留不删——它仍被别处/测试引用;这里改为用 `confirm_targets_pending` 统一判定,消除泛化词单独放行风险。原 import 行 `is_confirmation` 可保留。）

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_replay_security.py -q`
Expected: 全部通过(Task1 + Task2 用例)

Run: `.venv/Scripts/python.exe -m pytest tests/test_streaming_replay.py tests/test_consent.py -q 2>&1 | tail -5`
Expected: 既有确认流测试通过;若某测试依赖"裸'可以'触发重放"的旧行为,改为传 `confirm=True` 或提及单号(旧行为本身是被修复的 bug,断言语义应更新为新契约并在报告说明)

- [ ] **Step 6: 提交**

```bash
git add app/agent/consent.py app/api/streaming.py tests/test_replay_security.py
git commit -m "fix(P0): 确认信号绑定挂起动作——泛化词'可以/好的'不再单独触发重放,防误确认/错目标"
```

---

### Task 3: P1 限流键改 user_id(防伪造 session_id 绕过)

**Files:**
- Modify: `app/api/app.py`(限流调用,159 行)
- Test: `tests/test_rate_limit_key.py`(新)

**Interfaces:**
- Consumes: `_resolve_user` 已解析的 `user_id`(app.py:157);`rate_limiter.allow(key)`
- Produces: 无

- [ ] **Step 1: 写失败测试**

创建 `tests/test_rate_limit_key.py`:

```python
"""限流键:同一用户换 session_id 不应绕过限流(P1)。"""

from app.hardening.rate_limit import RateLimiter


def test_same_user_different_sessions_share_bucket():
    # 直接验证限流器语义:按 user_id 键,换 session 不重置
    rl = RateLimiter(max_per_window=2, window_seconds=60, now=lambda: 1000.0)
    assert rl.allow("user-A") is True
    assert rl.allow("user-A") is True
    assert rl.allow("user-A") is False        # 第3次被限,即使客户端换了 session
```

（此测试锁定"限流器本身按传入 key 计数"的语义;真正的键选择改在 app.py。再加一个集成断言确认 app.py 传的是 user_id——见 Step 3 的注释与 Step 5 冒烟。）

- [ ] **Step 2: 跑测试确认通过(限流器语义本就成立)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_rate_limit_key.py -q`
Expected: 1 passed（这是回归锁,防止后续误改限流器）

- [ ] **Step 3: 改 app.py 限流键**

`app/api/app.py` 把:

```python
    # 限流:每会话每分钟(用原始 session_id,不受 conversation 轮换影响)
    if not rate_limiter.allow(req.session_id):
```

替换为:

```python
    # 限流:按已鉴权 user_id(session_id 客户端可伪造,换 id 即绕过;user 从 token 解出不可伪造)。
    # auth 关闭时 _resolve_user 回退自报 id,退化为按自报身份限流,可接受
    _rl_key = user_id or req.session_id
    if not rate_limiter.allow(_rl_key):
```

- [ ] **Step 4: 全量导入自检**

Run: `.venv/Scripts/python.exe -c "import app.api.app; print('import ok')"`
Expected: `import ok`（`user_id` 已在 :157 定义,`_rl_key` 引用它;确认无 NameError）

Run: `.venv/Scripts/python.exe -m pytest tests/test_hardening_api.py -q 2>&1 | tail -5`
Expected: 既有限流/加固 API 测试通过(若测试固定用 session_id 触发限流且 auth 关,user_id 回退 session_id,行为不变)

- [ ] **Step 5: 提交**

```bash
git add app/api/app.py tests/test_rate_limit_key.py
git commit -m "fix(P1): 限流键改用鉴权user_id,防客户端伪造session_id绕过节流"
```

---

### Task 4: P1 意图对齐(trace 与 metadata 一致)

**Files:**
- Modify: `app/api/streaming.py`(`_normal_flow` 返回值,140 行)
- Test: `tests/test_intent_consistency.py`(新)

**Interfaces:**
- Consumes: `_normal_flow` 内已算好的 `intent_out`
- Produces: `_normal_flow` 返回 `intent_out`(而非 `result.intent.value`),使 `trace.intent`(app.py:204 消费返回值)与 metadata 事件一致

- [ ] **Step 1: 写失败测试**

创建 `tests/test_intent_consistency.py`:

```python
"""意图一致性:trace 记录的意图(_drive 返回值)必须与 metadata 事件一致(P1)。"""

import json
from types import SimpleNamespace

from app.api.streaming import run_agent_streaming
from app.config.settings import settings


class _Agent:
    def __init__(self):
        self.raw_messages = []
        self.user_id = "u1"
        self._pending = None
        self.client = None
        self._turn_qu = SimpleNamespace(intent="投诉", need_kb=False, domain=None,
                                        kb_query=None, source="llm")

    def set_turn_understanding(self, qu):
        self._turn_qu = qu

    def chat(self, user_input):
        from app.schemas.response import CustomerServiceResponse, IntentType
        # 生成侧意图漂移成 promotion,与 QU 的"投诉"不一致
        return CustomerServiceResponse(intent=IntentType.PROMOTION, confidence=0.9,
                                       reply="回复", requires_human=False,
                                       follow_up_question=None)


def test_trace_intent_matches_metadata(monkeypatch):
    monkeypatch.setattr(settings, "faq_cache_enabled", False)
    agent = _Agent()
    events = list(run_agent_streaming(agent, "你们太坑了", session_id="s1", hitl=None))
    meta = [e for e in events if e["type"] == "metadata"][0]
    # QU=投诉 覆盖 → metadata.intent 应为 complaint;_drive 返回值(trace.intent)也应是 complaint
    assert meta["intent"] == "complaint"
    # 复现 app.py 的消费:_drive 的返回值即 run_agent_streaming 内 trace.intent 来源
    # 这里用事件侧验证:normal_flow 返回 intent_out,与 metadata 同源
```

（说明:`_drive` 返回值在 worker 内赋给 `trace.intent`,不经事件流出;测试无法直接读 trace.intent,故通过"metadata.intent == complaint"锁定覆盖逻辑,并在实现里保证 `_normal_flow` 的 return 与 metadata 用同一个 `intent_out` 变量——见 Step 3。冒烟阶段用 Langfuse/自研 tracer 实查一次交叉验证。)

- [ ] **Step 2: 跑测试确认失败/通过基线**

Run: `.venv/Scripts/python.exe -m pytest tests/test_intent_consistency.py -q`
Expected: 若当前 metadata 已做投诉覆盖(Trio 阶段已加),此断言可能已 PASS;关键改动在 Step 3 让 return 对齐。先跑记录基线。

- [ ] **Step 3: 改 `_normal_flow` 返回值**

`app/api/streaming.py` `_normal_flow` 末尾把:

```python
        _sink({"type": "metadata", "intent": intent_out,
               "confidence": result.confidence,
               "requires_human": requires_human_out,
               "follow_up_question": result.follow_up_question})
        return result.intent.value
```

替换为:

```python
        _sink({"type": "metadata", "intent": intent_out,
               "confidence": result.confidence,
               "requires_human": requires_human_out,
               "follow_up_question": result.follow_up_question})
        return intent_out   # P1:trace.intent(消费本返回值)与 metadata 用同一覆盖后意图,消除三面漂移
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_intent_consistency.py tests/test_streaming_trace.py -q`
Expected: 全部通过(test_streaming_trace 若断言 trace.intent 具体值,确认新值仍匹配或更新为覆盖后值)

- [ ] **Step 5: 提交**

```bash
git add app/api/streaming.py tests/test_intent_consistency.py
git commit -m "fix(P1): _normal_flow返回覆盖后意图,trace与metadata意图对齐(消除三面漂移)"
```

---

### Task 5: P1 reaper 快照后释放锁再跑 LLM 巩固

**Files:**
- Modify: `app/api/session_manager.py`(`_consolidate_and_evict`,83-115 行)
- Test: `tests/test_session_reaper.py`(追加)

**Interfaces:**
- Consumes: `agent.save`/`agent.close`;`self.get_lock`/`self._guard`
- Produces: 巩固期间不再长时间持有 session 锁——锁只护"取 agent + 从字典摘除",LLM 巩固在锁外跑

- [ ] **Step 1: 写失败测试**(追加到 tests/test_session_reaper.py)

```python
def test_consolidation_runs_without_holding_session_lock(tmp_path, monkeypatch):
    """巩固期间(agent.close 里的慢操作)不得持有 session 锁,否则用户回来会被阻塞。"""
    import threading, time
    from app.api.session_manager import SessionManager

    lock_held_during_close = {"held": None}

    class SlowAgent:
        def __init__(self, p):
            self.session_path = p
        def save(self):
            pass
        def close(self):
            # 在 close(模拟慢 LLM 巩固)期间,探测 session 锁能否被别的线程拿到
            mgr_lock = self._mgr.get_lock("s1")
            lock_held_during_close["held"] = mgr_lock.locked()

    mgr = SessionManager(agent_factory=lambda p, u=None: SlowAgent(p),
                         clock=lambda: 1000.0)
    a = mgr.get_or_create("s1")
    a._mgr = mgr
    mgr._consolidate_and_evict("s1")
    assert lock_held_during_close["held"] is False   # close 跑时锁已释放
    assert "s1" not in mgr._agents                     # 仍完成回收
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_session_reaper.py::test_consolidation_runs_without_holding_session_lock -q`
Expected: FAIL（当前 `with lock:` 包住 close,`locked()` 为 True）

- [ ] **Step 3: 重构 `_consolidate_and_evict`**

`app/api/session_manager.py` 的 `_consolidate_and_evict` 整体替换为:

```python
    def _consolidate_and_evict(self, session_id: str) -> None:
        """巩固记忆(慢 LLM)在 session 锁外执行,避免用户回来时长时间阻塞;
        锁只护短临界区(取 agent / 从字典摘除)。失败不影响其它会话。"""
        from app.observability.langfuse_bridge import background_trace
        with self._guard:
            agent = self._agents.get(session_id)
        if agent is not None:
            # 后台巩固的 LLM 调用归到命名 trace 下——在锁外跑,不阻塞 /api/chat
            with background_trace("consolidate_memory", session_id=session_id,
                                  user_id=getattr(agent, "user_id", None),
                                  input={"session_id": session_id, "trigger": "idle_reaper"}):
                try:
                    if hasattr(agent, "save"):
                        agent.save()
                    if hasattr(agent, "close"):
                        agent.close()   # → memory_manager.consolidate_to_long_term(...)
                except Exception:
                    pass
            self._archiver.archive(session_id, agent)   # 冷归档(best-effort)
            from app.config.settings import settings
            if settings.conversation_idle_close_enabled:
                try:
                    from app.db import get_db
                    get_db().close_conversation(session_id, "idle")
                except Exception:
                    pass
        with self._guard:
            self._agents.pop(session_id, None)
            self._last_active.pop(session_id, None)
```

（关键变化:去掉外层 `with lock:`,巩固与归档在锁外;`self._guard` 仍护字典读写。语义权衡:巩固期间同会话若有新请求,可能与巩固并发——但 save/close 是幂等落盘 + 记忆抽取,且 reaper 只在空闲 TTL 后触发,并发概率极低,换来的是消除 60s 阻塞。此权衡写入报告。）

- [ ] **Step 4: 跑测试确认通过 + 既有 reaper 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_session_reaper.py -q`
Expected: 全部通过(既有"巩固+回收"语义不变,新增"锁外执行"成立)

- [ ] **Step 5: 提交**

```bash
git add app/api/session_manager.py tests/test_session_reaper.py
git commit -m "fix(P1): reaper巩固记忆移出session锁,消除用户回来时最长60s阻塞"
```

---

### Task 6: 回归 + 文档 + 浏览器安全流验收

**Files:**
- Modify: `docs/对标-Customer-Agent对比报告.md` 或新增 `docs/确认流安全修复-说明.md`(记录 6 项修复与权衡)
- Test: 回归子集 + 浏览器真实确认退款流

- [ ] **Step 1: 回归子集**

Run: `.venv/Scripts/python.exe -m pytest tests/test_replay_security.py tests/test_rate_limit_key.py tests/test_intent_consistency.py tests/test_session_reaper.py tests/test_streaming_replay.py tests/test_streaming_handoff.py tests/test_consent.py tests/test_hardening_api.py tests/test_tool_validation.py tests/test_idempotency.py -q`
Expected: 全部通过(test_rag/live 集成的预存失败不在此列)

- [ ] **Step 2: 写修复说明文档**

创建 `docs/确认流安全修复-说明.md`,记录:6 项问题的根因、修复、权衡(尤其 reaper 锁外巩固的并发权衡、限流 auth 关闭时退化行为、确认绑定的三条信号),以及"这批体检来自对标 Customer-Agent 后的自查"的来龙去脉。

- [ ] **Step 3: 浏览器真实确认退款流(协调者执行)**

前置:后端重启加载新代码;`auth_enabled=True`(默认);登录用户 123。
1. 查用户真实订单号(`list_user_orders` 或直接看库)
2. 发"我要退款 订单 ORD-xxx" → 应回"请确认"(pending 挂起)
3. 发"确认对 ORD-xxx 退款" → **应成功执行退款**(修复前会"未找到订单")
4. 反向验证:重新挂起一个退款后,发"好的"(泛化词)→ **不应执行退款**,应落正常对话
5. 记录两轮面板证据

- [ ] **Step 4: 提交**

```bash
git add docs/确认流安全修复-说明.md
git commit -m "docs: 确认流安全修复说明(6项P0/P1根因+权衡+浏览器验收)"
```
