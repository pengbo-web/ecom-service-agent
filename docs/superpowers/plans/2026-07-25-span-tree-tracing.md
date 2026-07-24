# 自研 Agent 链路追踪 Span 树(借 OTel GenAI 语义) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 把现有平铺 Trace 事件升级为**带父子层级和耗时的 Span 树**,回答"从用户发送到回复经历了哪些阶段、每段耗时多少",命名与属性**对齐 OTel GenAI 语义约定**(只借语义,不引 SDK),看板增加调用链树视图,并预留 OTLP 导出。

**Architecture:** 三层加法:①数据层——`Span` 增加 `parent_id`,`Tracer` 维护 per-trace 打开栈实现自动嵌套;②埋点层——复用现有 `event_sink`/`_emit` 事件通道新增 `stage` 事件(agent 代码零新依赖,streaming 已把所有事件桥接进 `tracer.on_event`);③展示层——`TracesTable` 详情弹窗渲染缩进树+耗时条。OTLP 导出做成纯函数 `trace_to_otlp()`,不接任何后端。

**Tech Stack:** Python 3.11 标准库(contextvars/sqlite3/uuid)、现有 observability 模块、React(webui)、vitest。

## Global Constraints

- **不引入任何第三方依赖**(观测层纯标准库;webui 不加新包)。
- **借 OTel GenAI 语义,不引 OTel SDK**:span 命名 `chat {model}` / `execute_tool {tool}` / `invoke_agent {agent}`;属性用 `gen_ai.*` 键(`gen_ai.request.model`、`gen_ai.tool.name`、`gen_ai.conversation.id`)放入 span `meta`。
- **kind 枚举向后兼容**:现有 `llm`/`tool`/`guard`/`hitl` 不变(metrics.py 按 kind 聚合),**新增** `stage`;metrics 不需要改。
- **事件协议只增不改**:新增 `{"type":"stage","status":"start"|"end","name":...,"attrs":{...}}` 事件;现有 thought/tool_call/select/evaluate/polish 等事件的字段与时机一字不动(前端 ChatView 白名单不含 stage,自动忽略,无需改前端聊天页)。
- **traces.db 向后兼容**:spans 表用 `ALTER TABLE ... ADD COLUMN parent_id TEXT` 迁移(PRAGMA table_info 检查后再加);旧数据 parent_id 为 NULL,树视图把 NULL/'' 当根级。
- **best-effort 铁律**:观测任何异常不影响对话主流程(现有 try/except 结构保持;stage 配对错乱时容错弹栈,不抛)。
- **并发安全**:打开栈与 pending tool 栈**必须挂在 Trace 实例上**(per-request),不能放 Tracer 实例(单例跨线程共享——现有 `_pending_tool` 在 Tracer 上是既有隐患,本次一并修复)。
- 门控:整个特性随现有 `settings.obs_enabled` 走,**不加新开关**(观测已有总开关,stage span 是其内部增强)。
- 每任务:独立提交 + 指定测试文件绿(**切勿跑全量 pytest**,含真网络用例会超时;逐文件跑)+ commit 末行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;推送 `origin feature/w1-service-streaming`,**绝不 push upstream**。
- 用 `.venv/Scripts/python.exe`,不要 conda base;测试不 print emoji(Windows gbk)。

## File Structure

| 文件 | 任务 | 责任 |
|---|---|---|
| `app/observability/trace.py`(改) | T1 | `Span.parent_id`/`attrs`;`Trace` 挂运行期栈(不持久化) |
| `app/observability/tracer.py`(改) | T1/T2 | 嵌套栈、stage 事件处理、pending tool 迁到 trace |
| `app/observability/store.py`(改) | T1 | spans 表 parent_id 迁移 + 读写 |
| `app/observability/client_proxy.py`(改) | T2 | LLM span 命名 `chat {model}` + `gen_ai.request.model` |
| `app/agent/chat.py`(改) | T3 | `react` stage 事件(_react_loop 前后) |
| `app/agent/reply_pipeline.py`(改) | T3 | `reply_pipeline`/`evaluate`/`redraft`/`polish` stage 事件 |
| `app/observability/otlp.py`(新) | T4 | `trace_to_otlp(trace_dict) -> dict` 纯函数 |
| `webui/src/lib/spanTree.ts`(新)+ `TracesTable.tsx`(改) | T5 | 树构建 + 缩进树/耗时条渲染 |
| `tests/test_tracer.py`(改)/`tests/test_span_tree.py`(新)/`tests/test_otlp_export.py`(新)/`webui/src/tests/span-tree.test.ts`(新) | T1-T5 | 各层测试 |

**最终 span 树形态**(复杂轮):

```
(trace 即 invoke_agent 根)
├─ route                    [stage, ~0ms, attrs{domain:"midsale"}]
├─ react                    [stage]
│   ├─ chat qwen-plus       [llm, tok 1200+80]
│   ├─ execute_tool query_order  [tool, success ✅]
│   └─ chat qwen-plus       [llm]
├─ reply_pipeline           [stage]
│   ├─ chat qwen-plus       [llm]   ← 选择器调用(在 reply_pipeline 下)
│   ├─ evaluate             [stage] └─ chat qwen-plus [llm]
│   └─ polish               [stage] └─ chat qwen-plus [llm]
├─ guard:contact_info       [guard, 0ms]
└─ handoff                  [hitl, 0ms]
```

---

### Task T1: Span 父子层级 + 嵌套栈 + 存储迁移

**Files:**
- Modify: `app/observability/trace.py`
- Modify: `app/observability/tracer.py`
- Modify: `app/observability/store.py`
- Test: `tests/test_tracer.py`(增)、`tests/test_trace_store.py`(增)

**Interfaces:**
- Consumes: 现有 `Trace/Span` dataclass、`Tracer.span(name, kind)`、`TraceStore.save_trace/get_trace`。
- Produces: `Span(parent_id: str = "")`;`Tracer.span(name, kind, attrs: dict | None = None)`(自动从当前打开栈取 parent、押栈/弹栈);`Trace._open_spans: list`、`Trace._pending_tool: list`(运行期字段,不入库);spans 表新列 `parent_id`。T2/T3 依赖嵌套栈;T4 依赖 parent_id 持久化。

- [ ] **Step 1: 写失败测试**(加到 `tests/test_tracer.py`)

```python
def test_nested_spans_get_parent_id(fake_store):
    tracer = Tracer(fake_store, now=FakeClock(), id_factory=seq_ids())
    with tracer.start_trace("s1", "hi") as trace:
        with tracer.span("react", "stage") as outer:
            with tracer.span("chat qwen-plus", "llm") as inner:
                pass
    assert inner.parent_id == outer.span_id
    assert outer.parent_id == ""            # 顶层挂根
    # 先开后关:children 先 append,顺序按 started_at 读即可

def test_sibling_spans_share_parent(fake_store):
    tracer = Tracer(fake_store, now=FakeClock(), id_factory=seq_ids())
    with tracer.start_trace("s1", "hi") as trace:
        with tracer.span("react", "stage") as outer:
            with tracer.span("a", "llm") as a:
                pass
            with tracer.span("b", "llm") as b:
                pass
    assert a.parent_id == outer.span_id and b.parent_id == outer.span_id

def test_open_stack_is_per_trace_not_on_tracer(fake_store):
    """并发安全:打开栈挂 Trace,不挂 Tracer 单例。"""
    tracer = Tracer(fake_store, now=FakeClock(), id_factory=seq_ids())
    with tracer.start_trace("s1", "hi") as t1:
        assert hasattr(t1, "_open_spans")
    assert not hasattr(tracer, "_open_spans")
```

(`fake_store`/`FakeClock`/`seq_ids` 如该文件已有夹具则复用;没有则:`fake_store`=有 `save_trace` 方法记录参数的简单对象,`FakeClock`=每次调用 +1.0 的可调用,`seq_ids`=生成 "id1","id2"... 的闭包。)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tracer.py -q`
Expected: FAIL(`Span.__init__` 无 parent_id / Trace 无 _open_spans)

- [ ] **Step 3: 实现 trace.py**

```python
@dataclass
class Span:
    span_id: str
    trace_id: str
    name: str
    kind: str            # "llm" | "tool" | "guard" | "hitl" | "stage"
    started_at: float
    ended_at: float
    latency_ms: float
    parent_id: str = ""              # 父 span;"" = 挂 trace 根
    success: Optional[bool] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    meta: dict = field(default_factory=dict)

@dataclass
class Trace:
    # ...原有字段不动,新增两个运行期字段(不持久化):
    spans: list = field(default_factory=list)
    _open_spans: list = field(default_factory=list, repr=False)    # 打开中的 span 栈(嵌套父子)
    _pending_tool: list = field(default_factory=list, repr=False)  # 未闭合的工具 span(T2 迁移用)
```

- [ ] **Step 4: 实现 tracer.py 的嵌套 span()**

```python
@contextmanager
def span(self, name: str, kind: str, attrs: dict | None = None):
    trace = _current.get()
    start = self._now()
    parent_id = trace._open_spans[-1].span_id if (trace and trace._open_spans) else ""
    sp = Span(span_id=self._id(), trace_id=trace.trace_id if trace else "",
              name=name, kind=kind, started_at=start, ended_at=start,
              latency_ms=0.0, parent_id=parent_id,
              meta=dict(attrs) if attrs else {})
    if trace is not None:
        trace._open_spans.append(sp)
    try:
        yield sp
    finally:
        sp.ended_at = self._now()
        sp.latency_ms = (sp.ended_at - sp.started_at) * 1000.0
        if trace is not None:
            if trace._open_spans and trace._open_spans[-1] is sp:
                trace._open_spans.pop()
            trace.spans.append(sp)
```

- [ ] **Step 5: store.py 迁移 + 持久化 parent_id**

`init_schema()` 的 executescript 后追加(建表语句本身也在 CREATE TABLE 里加 `parent_id TEXT`,兼顾新库):

```python
cols = {r[1] for r in conn.execute("PRAGMA table_info(spans)").fetchall()}
if "parent_id" not in cols:
    conn.execute("ALTER TABLE spans ADD COLUMN parent_id TEXT")
```

`save_trace()` 的 spans INSERT 增加 parent_id 列与 `s.parent_id` 值(11 列变 12 列,列名清单同步)。

测试(加到 `tests/test_trace_store.py`):

```python
def test_migration_adds_parent_id_to_existing_db(tmp_path):
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)     # 造一个旧 schema 的库(无 parent_id)
    conn.execute("CREATE TABLE spans (span_id TEXT PRIMARY KEY, trace_id TEXT, name TEXT,"
                 " kind TEXT, started_at REAL, ended_at REAL, latency_ms REAL,"
                 " success INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, meta TEXT)")
    conn.execute("CREATE TABLE traces (trace_id TEXT PRIMARY KEY, session_id TEXT,"
                 " user_input TEXT, intent TEXT, started_at REAL, ended_at REAL,"
                 " latency_ms REAL, status TEXT, error TEXT, prompt_tokens INTEGER,"
                 " completion_tokens INTEGER)")
    conn.commit(); conn.close()
    store = TraceStore(db); store.init_schema()      # 迁移不炸
    store.init_schema()                              # 幂等
    cols = {r[1] for r in store.connect().execute("PRAGMA table_info(spans)").fetchall()}
    assert "parent_id" in cols

def test_save_and_get_trace_roundtrips_parent_id(tmp_path):
    store = TraceStore(str(tmp_path / "t.db")); store.init_schema()
    t = Trace(trace_id="t1", session_id="s", user_input="q", intent=None,
              started_at=0, ended_at=1, latency_ms=1000, status="ok", error=None)
    t.spans = [Span("p1", "t1", "react", "stage", 0, 1, 1000),
               Span("c1", "t1", "chat m", "llm", 0, 1, 1000, parent_id="p1")]
    store.save_trace(t)
    got = store.get_trace("t1")
    by_id = {s["span_id"]: s for s in got["spans"]}
    assert by_id["c1"]["parent_id"] == "p1"
```

- [ ] **Step 6: 跑测试通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tracer.py tests/test_trace_store.py tests/test_metrics.py tests/test_dashboard_api.py -q`
Expected: PASS(metrics/dashboard 是回归确认)

- [ ] **Step 7: 提交**

```bash
git add app/observability/trace.py app/observability/tracer.py app/observability/store.py tests/test_tracer.py tests/test_trace_store.py
git commit -m "feat(obs): T1 span 父子层级+嵌套栈+traces.db parent_id 迁移"
```

---

### Task T2: stage 事件协议 + OTel 对齐命名

**Files:**
- Modify: `app/observability/tracer.py`(on_event)
- Modify: `app/observability/client_proxy.py`
- Test: `tests/test_tracer.py`(增)、`tests/test_client_proxy.py`(改断言)

**Interfaces:**
- Consumes: T1 的 `Trace._open_spans/_pending_tool`、`Span.parent_id`。
- Produces: `on_event` 支持 `{"type":"stage","status":"start"|"end","name":str,"attrs":dict?}`(start 开 span 押栈,end 弹栈闭合;名字不匹配容错弹顶);`route` 事件 → 瞬时 stage span(attrs `{"domain": key}`);tool span 更名 `execute_tool {name}` + meta 增 `gen_ai.tool.name`,pending 栈迁到 `trace._pending_tool`;LLM span 更名 `chat {model}` + meta `{"gen_ai.request.model": model}`。T3 靠 stage 事件埋点。

- [ ] **Step 1: 写失败测试**(加到 `tests/test_tracer.py`)

```python
def test_stage_events_build_nested_spans(fake_store):
    tracer = Tracer(fake_store, now=FakeClock(), id_factory=seq_ids())
    with tracer.start_trace("s1", "hi") as trace:
        tracer.on_event({"type": "stage", "status": "start", "name": "react"})
        tracer.on_event({"type": "tool_call", "name": "query_order", "args": {"order_id": "O1"}})
        tracer.on_event({"type": "tool_result", "content": '{"success": true}'})
        tracer.on_event({"type": "stage", "status": "end", "name": "react"})
    spans = {s.name: s for s in trace.spans}
    react = spans["react"]
    tool = spans["execute_tool query_order"]
    assert react.kind == "stage" and react.latency_ms > 0
    assert tool.parent_id == react.span_id
    assert tool.meta["gen_ai.tool.name"] == "query_order"

def test_stage_end_mismatch_is_tolerated(fake_store):
    tracer = Tracer(fake_store, now=FakeClock(), id_factory=seq_ids())
    with tracer.start_trace("s1", "hi") as trace:
        tracer.on_event({"type": "stage", "status": "start", "name": "a"})
        tracer.on_event({"type": "stage", "status": "end", "name": "WRONG"})   # 容错:弹顶,不抛
        tracer.on_event({"type": "stage", "status": "end", "name": "a"})       # 栈空:忽略,不抛
    assert any(s.name == "a" for s in trace.spans)

def test_route_event_becomes_stage_span(fake_store):
    tracer = Tracer(fake_store, now=FakeClock(), id_factory=seq_ids())
    with tracer.start_trace("s1", "hi") as trace:
        tracer.on_event({"type": "route", "agent": "售中客服", "key": "midsale"})
    sp = trace.spans[0]
    assert sp.name == "route" and sp.kind == "stage" and sp.meta["domain"] == "midsale"

def test_pending_tool_lives_on_trace_not_tracer(fake_store):
    tracer = Tracer(fake_store, now=FakeClock(), id_factory=seq_ids())
    with tracer.start_trace("s1", "hi") as trace:
        tracer.on_event({"type": "tool_call", "name": "t", "args": {}})
        assert len(trace._pending_tool) == 1
    assert not tracer._pending_tool        # 实例栈弃用后恒空(保留属性防旧引用炸)
```

`tests/test_client_proxy.py`:找到现断言 `llm.chat.create` 名字的用例,改为断言 `chat {model}`(fake create 传 `model="test-m"` → span 名 `chat test-m`,`meta["gen_ai.request.model"] == "test-m"`);`beta.parse` 路径名字用 `chat {model} (parse)`。

- [ ] **Step 2: 跑失败** → `pytest tests/test_tracer.py tests/test_client_proxy.py -q` FAIL

- [ ] **Step 3: 实现 tracer.on_event 增量**

```python
elif etype == "stage":
    if event.get("status") == "start":
        parent_id = trace._open_spans[-1].span_id if trace._open_spans else ""
        sp = Span(span_id=self._id(), trace_id=trace.trace_id,
                  name=str(event.get("name", "stage")), kind="stage",
                  started_at=self._now(), ended_at=0.0, latency_ms=0.0,
                  parent_id=parent_id, meta=dict(event.get("attrs") or {}))
        trace._open_spans.append(sp)
    else:                                  # end:容错弹顶(名字不匹配也弹,栈空忽略)
        if trace._open_spans:
            sp = trace._open_spans.pop()
            sp.ended_at = self._now()
            sp.latency_ms = (sp.ended_at - sp.started_at) * 1000.0
            trace.spans.append(sp)
elif etype == "route":
    now = self._now()
    parent_id = trace._open_spans[-1].span_id if trace._open_spans else ""
    trace.spans.append(Span(
        span_id=self._id(), trace_id=trace.trace_id, name="route", kind="stage",
        started_at=now, ended_at=now, latency_ms=0.0, parent_id=parent_id,
        meta={"domain": event.get("key"), "agent": event.get("agent")}))
```

同时:`tool_call` 分支改用 `trace._pending_tool`(名字 `f"execute_tool {event.get('name')}"`,meta 加 `"gen_ai.tool.name"`)并取 parent 同上;`tool_result` 从 `trace._pending_tool` 弹;`guard`/`handoff` 分支加 parent 取值(同一行逻辑)。`Tracer.__init__` 的 `self._pending_tool` 保留为空列表(兼容,不再使用)。

- [ ] **Step 4: 实现 client_proxy 命名**

```python
def create(self, **kwargs):
    model = kwargs.get("model", "")
    with self._tracer.span(f"chat {model}".strip(), "llm",
                           attrs={"gen_ai.request.model": model}) as sp:
        resp = self._real.create(**kwargs)
        _record_usage(sp, resp)
        return resp
```

(`parse` 同理,名字 `f"chat {model} (parse)".strip()`;`_TracedCompletions` 不再需要 span_name 参数——构造处同步删。)

- [ ] **Step 5: 跑通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tracer.py tests/test_client_proxy.py tests/test_metrics.py tests/test_streaming_trace.py -q`
Expected: PASS(`test_streaming_trace` 若断言旧 span 名,同步改为新名——这是**本任务范围内**的适配)

- [ ] **Step 6: 提交**

```bash
git add app/observability/tracer.py app/observability/client_proxy.py tests/
git commit -m "feat(obs): T2 stage 事件协议 + OTel GenAI 对齐命名(chat/execute_tool/route)"
```

---

### Task T3: 埋点——react / reply_pipeline / evaluate / redraft / polish

**Files:**
- Modify: `app/agent/chat.py`(`chat()` 内 `_react_loop` 前后)
- Modify: `app/agent/reply_pipeline.py`(`run()` 与 `_evaluate/_redraft/_polish` 前后)
- Test: `tests/test_span_tree.py`(新,集成:fake client 驱动一轮断言树形)

**Interfaces:**
- Consumes: T2 的 stage 事件协议;现有 `self._emit(dict)`(chat.py)与 `_emit(emit, dict)`(reply_pipeline.py,emit 为 None 时跳过)。
- Produces: 事件序列(agent 侧只发事件,不接触 Tracer):
  - chat():`stage start react` → …react 循环… → `stage end react`
  - ReplyPipeline.run():过门控后 `stage start reply_pipeline`,`finally` 里 `stage end reply_pipeline`
  - `_evaluate/_redraft/_polish`:各自 LLM 调用前 `stage start {evaluate|redraft|polish}`、完成后(含 fail-open 路径,用 try/finally)`stage end ...`

- [ ] **Step 1: 写失败测试** `tests/test_span_tree.py`

```python
"""集成:一轮复杂对话的事件流经 Tracer 后形成正确的 span 树。
用「事件重放」方式:不起真 agent,按 chat()/pipeline 的埋点顺序重放事件,
断言树形——这同时锁定了埋点协议与 tracer 组树两端。"""

from app.observability.tracer import Tracer

def test_full_turn_event_replay_builds_expected_tree(fake_store):
    tracer = Tracer(fake_store, now=FakeClock(), id_factory=seq_ids())
    with tracer.start_trace("s1", "查订单") as trace:
        tracer.on_event({"type": "route", "agent": "售中", "key": "midsale"})
        tracer.on_event({"type": "stage", "status": "start", "name": "react"})
        with tracer.span("chat test-m", "llm"):
            pass
        tracer.on_event({"type": "tool_call", "name": "query_order", "args": {}})
        tracer.on_event({"type": "tool_result", "content": '{"success": true}'})
        with tracer.span("chat test-m", "llm"):
            pass
        tracer.on_event({"type": "stage", "status": "end", "name": "react"})
        tracer.on_event({"type": "stage", "status": "start", "name": "reply_pipeline"})
        tracer.on_event({"type": "stage", "status": "start", "name": "evaluate"})
        with tracer.span("chat test-m", "llm"):
            pass
        tracer.on_event({"type": "stage", "status": "end", "name": "evaluate"})
        tracer.on_event({"type": "stage", "status": "end", "name": "reply_pipeline"})
    by_name = {}
    for s in trace.spans:
        by_name.setdefault(s.name, []).append(s)
    react = by_name["react"][0]
    rp = by_name["reply_pipeline"][0]
    ev = by_name["evaluate"][0]
    assert by_name["execute_tool query_order"][0].parent_id == react.span_id
    assert all(c.parent_id == react.span_id for c in by_name["chat test-m"][:2])
    assert ev.parent_id == rp.span_id
    assert by_name["chat test-m"][2].parent_id == ev.span_id
    assert react.parent_id == "" and rp.parent_id == ""

def test_agent_chat_emits_react_stage_events(tmp_path, monkeypatch):
    """真 agent(裸构造,monkeypatch _react_loop)确实发 react stage 事件对。"""
    from app.agent.chat import EcomAgent
    from app.config.settings import settings
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path))
    a = EcomAgent(session_path=str(tmp_path / "s.json"), user_id="u1")
    events = []
    a.event_sink = lambda e: events.append(e)
    monkeypatch.setattr(a, "_react_loop",
        lambda: '{"intent":"other","confidence":0.9,"reply":"ok","requires_human":false}')
    monkeypatch.setattr(a.memory_manager, "update_short_term", lambda *_: None)
    a.chat("你好")
    stages = [(e.get("status"), e.get("name")) for e in events if e.get("type") == "stage"]
    assert ("start", "react") in stages and ("end", "react") in stages
    assert stages.index(("start", "react")) < stages.index(("end", "react"))

def test_pipeline_emits_stage_pairs_for_each_role(monkeypatch):
    """流水线对 evaluate/polish 各发一对 stage 事件(fail-open 路径也要闭合)。"""
    import json as _json
    from app.agent.reply_pipeline import ReplyPipeline
    from app.config.settings import settings
    monkeypatch.setattr(settings, "reply_pipeline_enabled", True)
    monkeypatch.setattr(settings, "selector_mode", "rule")
    monkeypatch.setattr(settings, "reply_pipeline_max_rounds", 2)
    client = FakeClient([_json.dumps({"ok": True, "issues": [], "suggestion": ""}), "润色文本"])
    events = []
    ReplyPipeline().run(client=client, model="m", user_input="q", draft="d",
                        grounding="g", complex_turn=True, emit=lambda e: events.append(e))
    stages = [(e.get("status"), e.get("name")) for e in events if e.get("type") == "stage"]
    for name in ("reply_pipeline", "evaluate", "polish"):
        assert ("start", name) in stages and ("end", name) in stages
```

(FakeClient 复用 `tests/test_reply_pipeline.py` 的同款脚本化实现——在本文件重新定义或 import。)

- [ ] **Step 2: 跑失败** → `pytest tests/test_span_tree.py -q` FAIL(无 stage 事件)

- [ ] **Step 3: chat.py 埋点**(只加 3 行,`chat()` 里)

```python
self._emit({"type": "stage", "status": "start", "name": "react"})
try:
    final_text = self._react_loop()
finally:
    self._emit({"type": "stage", "status": "end", "name": "react"})
```

- [ ] **Step 4: reply_pipeline.py 埋点**

`run()` 在两个门控 return 之后、主循环之前发 start;整个循环包 try/finally 发 end:

```python
_emit(emit, {"type": "stage", "status": "start", "name": "reply_pipeline"})
try:
    ...主循环(原样)...
finally:
    _emit(emit, {"type": "stage", "status": "end", "name": "reply_pipeline"})
return state["polished"] if ...
```

`_evaluate/_redraft/_polish` 各自开头/结尾(fail-open 分支也要走到 end——把函数体包 try/finally):

```python
def _evaluate(self, client, model, state, emit) -> None:
    _emit(emit, {"type": "stage", "status": "start", "name": "evaluate"})
    try:
        ...原逻辑(含 fail-open except)...
    finally:
        _emit(emit, {"type": "stage", "status": "end", "name": "evaluate"})
```

(redraft/polish 同构,名字换成 `redraft`/`polish`。注意 end 事件在原有 `evaluate`/`polish` 业务事件**之后**发也可以——tracer 只按 start/end 配对。)

- [ ] **Step 5: 跑通过 + 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_span_tree.py tests/test_reply_pipeline.py tests/test_chat_pipeline_wiring.py tests/test_react_degrade.py tests/test_emit.py tests/test_streaming_trace.py -q`
Expected: PASS(若 test_emit/streaming 有"事件序列精确相等"断言被新增 stage 事件破坏,改断言为过滤式/包含式——事件协议是"只增",精确相等断言本身过脆)

- [ ] **Step 6: 提交**

```bash
git add app/agent/chat.py app/agent/reply_pipeline.py tests/test_span_tree.py tests/
git commit -m "feat(obs): T3 react/reply_pipeline/evaluate/redraft/polish 阶段埋点(stage 事件)"
```

---

### Task T4: OTLP 导出预留(纯函数)

**Files:**
- Create: `app/observability/otlp.py`
- Test: `tests/test_otlp_export.py`(新)

**Interfaces:**
- Consumes: `TraceStore.get_trace(trace_id)` 返回的 dict(含 `spans` list,每个含 `parent_id`)。
- Produces: `trace_to_otlp(trace: dict, service_name: str = "ecom-service-agent") -> dict`——OTLP/JSON `{"resourceSpans": [...]}`,供未来 POST 到任意 OTLP HTTP collector(本任务**不做**网络发送)。

- [ ] **Step 1: 写失败测试**

```python
from app.observability.otlp import trace_to_otlp

def _sample_trace():
    return {
        "trace_id": "abc123", "session_id": "s1", "user_input": "查订单",
        "intent": "order_query", "started_at": 100.0, "ended_at": 105.5,
        "latency_ms": 5500.0, "status": "ok", "error": None,
        "spans": [
            {"span_id": "p1", "parent_id": "", "name": "react", "kind": "stage",
             "started_at": 100.1, "ended_at": 105.0, "latency_ms": 4900.0,
             "success": None, "prompt_tokens": 0, "completion_tokens": 0, "meta": "{}"},
            {"span_id": "c1", "parent_id": "p1", "name": "chat test-m", "kind": "llm",
             "started_at": 100.2, "ended_at": 102.0, "latency_ms": 1800.0,
             "success": None, "prompt_tokens": 120, "completion_tokens": 30,
             "meta": '{"gen_ai.request.model": "test-m"}'},
        ],
    }

def test_otlp_structure_and_root_span():
    out = trace_to_otlp(_sample_trace())
    scope_spans = out["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root = next(s for s in scope_spans if s["name"].startswith("invoke_agent"))
    assert root["parentSpanId"] == ""
    react = next(s for s in scope_spans if s["name"] == "react")
    assert react["parentSpanId"] == root["spanId"]     # 顶层 span 挂合成根
    chat = next(s for s in scope_spans if s["name"] == "chat test-m")
    assert chat["parentSpanId"] == react["spanId"]

def test_otlp_ids_are_hex_padded_and_times_nanos():
    out = trace_to_otlp(_sample_trace())
    spans = out["resourceSpans"][0]["scopeSpans"][0]["spans"]
    for s in spans:
        assert len(s["traceId"]) == 32 and len(s["spanId"]) == 16
        assert s["endTimeUnixNano"] >= s["startTimeUnixNano"]
    chat = next(s for s in spans if s["name"] == "chat test-m")
    keys = {a["key"] for a in chat["attributes"]}
    assert "gen_ai.request.model" in keys

def test_otlp_resource_and_conversation_attrs():
    out = trace_to_otlp(_sample_trace())
    res_attrs = {a["key"]: a["value"]["stringValue"]
                 for a in out["resourceSpans"][0]["resource"]["attributes"]}
    assert res_attrs["service.name"] == "ecom-service-agent"
    spans = out["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root = next(s for s in spans if s["name"].startswith("invoke_agent"))
    root_attrs = {a["key"] for a in root["attributes"]}
    assert "gen_ai.conversation.id" in root_attrs
```

- [ ] **Step 2: 跑失败** → `pytest tests/test_otlp_export.py -q` FAIL(模块不存在)

- [ ] **Step 3: 实现 otlp.py**

```python
"""OTLP/JSON 导出(预留):把自研 trace dict 转成 OTLP ResourceSpans 结构。

只做纯转换,不发网络请求——将来要接 Jaeger/Langfuse/云厂商时,
POST 本函数输出到 OTLP HTTP collector(/v1/traces)即可,埋点零改动。
命名/属性对齐 OTel GenAI 语义约定:根 span=invoke_agent,gen_ai.* 属性。
"""

import json


def _hex_pad(raw: str, width: int) -> str:
    h = "".join(ch for ch in str(raw) if ch in "0123456789abcdef") or "0"
    return (h * (width // len(h) + 1))[:width]


def _nanos(ts: float) -> int:
    return int(ts * 1_000_000_000)


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def trace_to_otlp(trace: dict, service_name: str = "ecom-service-agent") -> dict:
    trace_id = _hex_pad(trace["trace_id"], 32)
    root_id = _hex_pad("root" + trace["trace_id"], 16)
    root = {
        "traceId": trace_id, "spanId": root_id, "parentSpanId": "",
        "name": "invoke_agent 小夕", "kind": 1,
        "startTimeUnixNano": _nanos(trace["started_at"]),
        "endTimeUnixNano": _nanos(trace["ended_at"]),
        "attributes": [
            _attr("gen_ai.operation.name", "invoke_agent"),
            _attr("gen_ai.agent.name", "小夕"),
            _attr("gen_ai.conversation.id", trace.get("session_id") or ""),
            _attr("ecom.intent", trace.get("intent") or ""),
        ],
        "status": {"code": 1 if trace.get("status") == "ok" else 2},
    }
    spans = [root]
    for s in trace.get("spans", []):
        meta = s.get("meta")
        meta = json.loads(meta) if isinstance(meta, str) and meta else (meta or {})
        attrs = [_attr(k, v) for k, v in meta.items()]
        attrs.append(_attr("ecom.span.kind", s.get("kind", "")))
        if s.get("prompt_tokens"):
            attrs.append(_attr("gen_ai.usage.input_tokens", s["prompt_tokens"]))
            attrs.append(_attr("gen_ai.usage.output_tokens", s.get("completion_tokens", 0)))
        spans.append({
            "traceId": trace_id,
            "spanId": _hex_pad(s["span_id"], 16),
            "parentSpanId": _hex_pad(s["parent_id"], 16) if s.get("parent_id") else root_id,
            "name": s.get("name", ""), "kind": 1,
            "startTimeUnixNano": _nanos(s["started_at"]),
            "endTimeUnixNano": _nanos(s["ended_at"]),
            "attributes": attrs,
            "status": {"code": 2 if s.get("success") == 0 else 1},
        })
    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", service_name)]},
        "scopeSpans": [{"scope": {"name": "ecom.observability"}, "spans": spans}],
    }]}
```

- [ ] **Step 4: 跑通过** → `pytest tests/test_otlp_export.py -q` PASS

- [ ] **Step 5: 提交**

```bash
git add app/observability/otlp.py tests/test_otlp_export.py
git commit -m "feat(obs): T4 OTLP/JSON 导出预留(纯函数,对齐 GenAI 语义)"
```

---

### Task T5: 看板调用链树视图 + 端到端冒烟

**Files:**
- Create: `webui/src/lib/spanTree.ts`
- Modify: `webui/src/components/TracesTable.tsx`
- Test: `webui/src/tests/span-tree.test.ts`(新)
- 构建: `cd webui && npm run build`(产 `web/dist`,提交)

**Interfaces:**
- Consumes: `GET /api/traces/{id}` 返回的 `spans[]`(现在含 `parent_id`;API 层**零改动**——`SELECT *` 自动带出新列)。
- Produces: `buildSpanTree(spans: SpanRow[]): SpanNode[]`(树数组,`SpanNode = SpanRow & { children: SpanNode[]; depth: number }`);TracesTable 详情弹窗渲染缩进树 + 耗时条。

- [ ] **Step 1: 写失败测试** `webui/src/tests/span-tree.test.ts`

```ts
import { describe, it, expect } from "vitest";
import { buildSpanTree } from "@/lib/spanTree";

const spans = [
  { span_id: "p1", parent_id: "", name: "react", kind: "stage", started_at: 1, latency_ms: 100 },
  { span_id: "c1", parent_id: "p1", name: "chat m", kind: "llm", started_at: 2, latency_ms: 50 },
  { span_id: "c2", parent_id: "p1", name: "execute_tool q", kind: "tool", started_at: 3, latency_ms: 10 },
  { span_id: "x1", parent_id: null, name: "route", kind: "stage", started_at: 0.5, latency_ms: 0 },
  { span_id: "orphan", parent_id: "GONE", name: "lost", kind: "llm", started_at: 4, latency_ms: 1 },
];

describe("buildSpanTree", () => {
  it("nests children under parents, roots ordered by started_at", () => {
    const tree = buildSpanTree(spans as any);
    expect(tree.map((n) => n.name)).toEqual(["route", "react", "lost"]); // 孤儿提升为根
    const react = tree[1];
    expect(react.children.map((c) => c.name)).toEqual(["chat m", "execute_tool q"]);
    expect(react.children[0].depth).toBe(1);
  });
  it("null/empty parent_id are both roots (旧数据兼容)", () => {
    const tree = buildSpanTree(spans as any);
    expect(tree.every((n) => n.depth === 0)).toBe(true);
  });
});
```

- [ ] **Step 2: 跑失败** → `cd webui && npx vitest run src/tests/span-tree.test.ts` FAIL

- [ ] **Step 3: 实现 spanTree.ts**

```ts
export type SpanRow = {
  span_id: string; parent_id?: string | null; name: string; kind: string;
  started_at: number; latency_ms?: number; success?: number | boolean | null;
  prompt_tokens?: number; completion_tokens?: number;
};
export type SpanNode = SpanRow & { children: SpanNode[]; depth: number };

export function buildSpanTree(spans: SpanRow[]): SpanNode[] {
  const nodes = new Map<string, SpanNode>();
  for (const s of spans) nodes.set(s.span_id, { ...s, children: [], depth: 0 });
  const roots: SpanNode[] = [];
  for (const n of nodes.values()) {
    const pid = n.parent_id || "";
    const parent = pid ? nodes.get(pid) : undefined;
    if (parent) parent.children.push(n);
    else roots.push(n);                       // ''/null/孤儿(父不存在)都提升为根
  }
  const sort = (arr: SpanNode[], depth: number) => {
    arr.sort((a, b) => a.started_at - b.started_at);
    for (const n of arr) { n.depth = depth; sort(n.children, depth + 1); }
  };
  sort(roots, 0);
  return roots;
}

export function flatten(tree: SpanNode[]): SpanNode[] {
  const out: SpanNode[] = [];
  const walk = (n: SpanNode) => { out.push(n); n.children.forEach(walk); };
  tree.forEach(walk);
  return out;
}
```

- [ ] **Step 4: TracesTable 详情弹窗改树渲染**

替换 `<pre>` 里 spans 的平铺拼接为(Span type 增 `span_id/parent_id/started_at` 字段声明):

```tsx
import { buildSpanTree, flatten } from "@/lib/spanTree";

const KIND_COLOR: Record<string, string> = {
  stage: "text-primary", llm: "text-accent-foreground",
  tool: "text-amber-600", guard: "text-destructive", hitl: "text-purple-600",
};

// Dialog 内,用户/意图行保留,spans 部分改为:
{detail?.spans && (() => {
  const rows = flatten(buildSpanTree(detail.spans as any));
  const total = Math.max(...rows.map((r) => r.latency_ms ?? 0), 1);
  return (
    <div className="mt-3 max-h-[60vh] overflow-auto rounded-md bg-secondary p-3 text-xs font-mono">
      {rows.map((s) => (
        <div key={s.span_id} className="flex items-center gap-2 py-0.5">
          <span style={{ paddingLeft: s.depth * 16 }} className={KIND_COLOR[s.kind] ?? ""}>
            {s.depth > 0 ? "└ " : ""}{s.name}
          </span>
          <span className="ml-auto shrink-0 text-muted-foreground">
            {(s.latency_ms ?? 0).toFixed(0)}ms
            {s.success == null ? "" : s.success ? " OK" : " FAIL"}
            {s.prompt_tokens ? ` tok:${s.prompt_tokens}+${s.completion_tokens}` : ""}
          </span>
          <div className="h-1.5 w-24 shrink-0 rounded bg-background">
            <div className="h-1.5 rounded bg-primary"
                 style={{ width: `${Math.min(100, ((s.latency_ms ?? 0) / total) * 100)}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
})()}
```

- [ ] **Step 5: 前端测试 + 构建**

Run: `cd webui && npx vitest run && npm run build`
Expected: 全部 PASS;`web/dist` 更新

- [ ] **Step 6: 端到端冒烟(控制方执行)**

起服务(`.venv` uvicorn)→ 前端问"帮我查一下订单 ORD-20240115-001 的状态"(复杂轮)→ 看板 Tab → 点最新 trace → 弹窗应呈现:
`route(0ms) / react(└ chat {model}, └ execute_tool query_order, └ chat {model}) / reply_pipeline(└ chat…, └ evaluate└chat, └ polish└chat)`,各行有耗时与占比条。

- [ ] **Step 7: 提交**

```bash
git add webui/src web/dist
git commit -m "feat(webui): T5 看板调用链树视图(缩进树+耗时条)"
```

---

## 总量与顺序

T1(数据层,~0.4d)→ T2(协议+命名,~0.4d)→ T3(埋点,~0.4d)→ T4(OTLP 预留,~0.3d)→ T5(看板+冒烟,~0.5d),共 **~2 人日**。严格顺序依赖,不可并行。

## Self-Review

- **覆盖核对**:span 树(T1-T3)✅;OTel 语义对齐(T2 命名/属性、T4 invoke_agent 根+gen_ai.*)✅;11 阶段中有耗时意义的全部入树(route/react/chat/execute_tool/reply_pipeline/evaluate/redraft/polish/guard/handoff;fast-path 与确认轮发生在 start_trace 之前/之外,快路径本就 <1ms 且不进模型,**明确不埋**——YAGNI);看板树视图(T5)✅;OTLP 预留(T4)✅;并发隐患修复(_pending_tool→trace)✅。
- **占位符扫描**:无 TBD/无"类似 Task N";所有代码块完整可抄。
- **类型一致性**:`Span.parent_id: str = ""` 贯穿 T1(模型/存储)/T2(on_event)/T4(otlp 读 dict)/T5(ts 类型含 null 兼容);`stage` 事件字段 `status/name/attrs` 在 T2 定义、T3 发射、T2 测试消费一致;`buildSpanTree/flatten` 名称在 T5 测试与实现一致。
- **既有测试风险点已标注**:T2 的 `test_streaming_trace`/`test_client_proxy` 旧 span 名断言、T3 的事件精确相等断言,均写明"本任务范围内适配"。
