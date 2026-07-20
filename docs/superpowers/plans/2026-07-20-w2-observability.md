# W2：全链路可观测性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每次对话请求生成一条 Trace（含每次 LLM 调用与每次工具调用的 Span），采集延迟、token、成功/失败，持久化到独立 SQLite，并提供一个指标看板页（延迟 P50/P95、token 成本、工具成功率、意图分布、错误、最近 Trace 明细）。

**Architecture:** **全部埋点在服务层，`chat.py` 零改动**（严守"不动核心"）。三条采集路径：① 请求级 Trace —— 在 `run_agent_streaming` 的后台线程用 `start_trace()` 包住整次 `agent.chat()`；② 工具 Span —— 复用 W1 的 `event_sink`，用 `tracer.on_event` 拦截 `tool_call`/`tool_result` 事件配对成 span；③ LLM Span + token —— 用一个**透明代理 `TracingClient`** 临时包装 `agent.client`（与 event_sink 相同的运行时注入范式），拦截 `chat.completions.create` 记录 `response.usage` 与延迟。"当前 Trace"用 `contextvars.ContextVar` 传递，均在 worker 线程内生效。

**Tech Stack:** Python 3.11+、`sqlite3`、`contextvars`、FastAPI（新增看板与指标端点）、原生 HTML/JS 看板、pytest（离线，用 fake client/agent）。

## Global Constraints

- **Python 3.11+**；解释器 `D:/2026项目/ecom-service-agent/.venv/Scripts/python.exe`。
- **`chat.py` / `orchestrator.py` 核心逻辑零改动**：埋点只允许出现在 `app/observability/` 与 `app/api/`（服务层）。
- **可观测性可开关**：`settings.obs_enabled`（默认 True）；关闭时 `run_agent_streaming` 行为与 W1 完全一致。
- **不破坏 W1**：`run_agent_streaming` 新增参数必须有默认值，`tracer=None` 时行为与 W1 一致；W1 既有测试须继续通过。
- **测试离线**：用 fake client/agent + 临时 trace 库，不依赖 `OPENAI_API_KEY`、网络、真实 traces.db。
- **Trace 存独立库**：`settings.trace_db_path`（默认 `app/sessions/traces.db`，已被 .gitignore）。
- **成本估算可配置**：token 单价用 `settings` 常量，估算值仅供参考。
- **Windows 友好**：`pathlib`。

---

## File Structure

- `app/config/settings.py` — 修改：新增 `obs_enabled` / `trace_db_path` / token 单价。
- `app/observability/__init__.py` — 新建：导出 `Trace` / `Span` / `Tracer` / `TraceStore` / `get_tracer` / `set_tracer`。
- `app/observability/trace.py` — 新建：`Trace` / `Span` 数据模型。
- `app/observability/store.py` — 新建：`TraceStore`（SQLite 建表 + 存 + 查）。
- `app/observability/tracer.py` — 新建：`Tracer`（contextvar 当前 trace、`start_trace`、`span`、`on_event`）。
- `app/observability/client_proxy.py` — 新建：`TracingClient` 透明代理。
- `app/observability/metrics.py` — 新建：聚合指标。
- `app/api/streaming.py` — 修改：`run_agent_streaming` 接入 tracer（可选参数）。
- `app/api/app.py` — 修改：装配 tracer；新增 `/api/metrics`、`/api/traces`、`/api/traces/{id}`、`/dashboard`。
- `web/dashboard.html` — 新建：指标看板页。
- `tests/test_trace_store.py` / `test_tracer.py` / `test_client_proxy.py` / `test_streaming_trace.py` / `test_metrics.py` / `test_dashboard_api.py` — 新建。

---

## Task 1: Trace/Span 模型 + SQLite 存储

**Files:**
- Modify: `app/config/settings.py`
- Create: `app/observability/trace.py`
- Create: `app/observability/store.py`
- Create: `app/observability/__init__.py`
- Test: `tests/test_trace_store.py`

**Interfaces:**
- Produces:
  - `settings.obs_enabled: bool`、`settings.trace_db_path: str`、`settings.price_per_1k_prompt: float`、`settings.price_per_1k_completion: float`
  - `trace.py`：
    - `@dataclass Span`: `span_id:str, trace_id:str, name:str, kind:str, started_at:float, ended_at:float, latency_ms:float, success:Optional[bool], prompt_tokens:int, completion_tokens:int, meta:dict`
    - `@dataclass Trace`: `trace_id:str, session_id:str, user_input:str, intent:Optional[str], started_at:float, ended_at:float, latency_ms:float, status:str, error:Optional[str], spans:list[Span]` + 属性 `prompt_tokens`/`completion_tokens`（对 spans 求和）
  - `store.py`：`class TraceStore(db_path=None)`：`init_schema()`、`save_trace(trace)`、`recent_traces(limit=20)->list[dict]`、`get_trace(trace_id)->Optional[dict]`、`all_traces()->list[dict]`、`all_spans()->list[dict]`
  - `__init__.py`：导出上述 + `get_tracer()/set_tracer()`（Task 2 填充，先占位可空）

- [ ] **Step 1: 写失败测试**

`tests/test_trace_store.py`：
```python
from app.observability.trace import Trace, Span
from app.observability.store import TraceStore


def _make_trace():
    spans = [
        Span("s1", "t1", "llm.chat.create", "llm", 1.0, 1.5, 500.0, None, 100, 20, {}),
        Span("s2", "t1", "tool:query_order", "tool", 1.5, 1.6, 100.0, True, 0, 0, {"name": "query_order"}),
    ]
    return Trace("t1", "sess1", "查订单", "order_query", 1.0, 1.7, 700.0, "ok", None, spans)


def test_trace_token_sums():
    t = _make_trace()
    assert t.prompt_tokens == 100
    assert t.completion_tokens == 20


def test_save_and_get_trace(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    store.save_trace(_make_trace())

    got = store.get_trace("t1")
    assert got["trace_id"] == "t1"
    assert got["intent"] == "order_query"
    assert len(got["spans"]) == 2
    assert got["prompt_tokens"] == 100


def test_recent_traces(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    store.save_trace(_make_trace())
    rows = store.recent_traces(limit=10)
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "t1"
    assert rows[0]["status"] == "ok"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_trace_store.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.observability'`。

- [ ] **Step 3: 实现**

`app/config/settings.py` 在 `db_path` 行下方加：
```python
    # 可观测性（W2）
    obs_enabled: bool = True
    trace_db_path: str = "app/sessions/traces.db"
    price_per_1k_prompt: float = 0.0015      # 成本估算（美元/1k tokens，仅参考）
    price_per_1k_completion: float = 0.002
```
`app/observability/trace.py`：
```python
"""Trace / Span 数据模型。"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Span:
    span_id: str
    trace_id: str
    name: str
    kind: str            # "llm" | "tool"
    started_at: float
    ended_at: float
    latency_ms: float
    success: Optional[bool] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    meta: dict = field(default_factory=dict)


@dataclass
class Trace:
    trace_id: str
    session_id: str
    user_input: str
    intent: Optional[str]
    started_at: float
    ended_at: float
    latency_ms: float
    status: str          # "ok" | "error"
    error: Optional[str]
    spans: list = field(default_factory=list)

    @property
    def prompt_tokens(self) -> int:
        return sum(s.prompt_tokens for s in self.spans)

    @property
    def completion_tokens(self) -> int:
        return sum(s.completion_tokens for s in self.spans)
```
`app/observability/store.py`：
```python
"""Trace 持久化（独立 SQLite）。"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from app.config.settings import settings
from app.observability.trace import Trace, Span


class TraceStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.trace_db_path

    def connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    user_input TEXT,
                    intent TEXT,
                    started_at REAL,
                    ended_at REAL,
                    latency_ms REAL,
                    status TEXT,
                    error TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER
                );
                CREATE TABLE IF NOT EXISTS spans (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT,
                    name TEXT,
                    kind TEXT,
                    started_at REAL,
                    ended_at REAL,
                    latency_ms REAL,
                    success INTEGER,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    meta TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
                CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def save_trace(self, trace: Trace) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO traces
                   (trace_id, session_id, user_input, intent, started_at, ended_at,
                    latency_ms, status, error, prompt_tokens, completion_tokens)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trace.trace_id, trace.session_id, trace.user_input, trace.intent,
                 trace.started_at, trace.ended_at, trace.latency_ms, trace.status,
                 trace.error, trace.prompt_tokens, trace.completion_tokens),
            )
            for s in trace.spans:
                conn.execute(
                    """INSERT OR REPLACE INTO spans
                       (span_id, trace_id, name, kind, started_at, ended_at, latency_ms,
                        success, prompt_tokens, completion_tokens, meta)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (s.span_id, s.trace_id, s.name, s.kind, s.started_at, s.ended_at,
                     s.latency_ms,
                     None if s.success is None else int(s.success),
                     s.prompt_tokens, s.completion_tokens,
                     json.dumps(s.meta, ensure_ascii=False)),
                )
            conn.commit()
        finally:
            conn.close()

    def recent_traces(self, limit: int = 20) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM traces ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def all_traces(self) -> list[dict]:
        conn = self.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM traces").fetchall()]
        finally:
            conn.close()

    def all_spans(self) -> list[dict]:
        conn = self.connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM spans").fetchall()]
        finally:
            conn.close()

    def get_trace(self, trace_id: str) -> Optional[dict]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
            if not row:
                return None
            trace = dict(row)
            spans = conn.execute(
                "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at", (trace_id,)
            ).fetchall()
            trace["spans"] = [dict(s) for s in spans]
            return trace
        finally:
            conn.close()
```
`app/observability/__init__.py`：
```python
from app.observability.trace import Trace, Span
from app.observability.store import TraceStore

_TRACER = None


def get_tracer():
    return _TRACER


def set_tracer(tracer) -> None:
    global _TRACER
    _TRACER = tracer


__all__ = ["Trace", "Span", "TraceStore", "get_tracer", "set_tracer"]
```

- [ ] **Step 4: 运行确认通过**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_trace_store.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: 提交**

```bash
git add app/config/settings.py app/observability/trace.py app/observability/store.py app/observability/__init__.py tests/test_trace_store.py
git commit -m "feat(obs): Trace/Span 模型 + SQLite 存储"
```

---

## Task 2: Tracer（contextvar 当前 trace + span + on_event）

**Files:**
- Create: `app/observability/tracer.py`
- Modify: `app/observability/__init__.py`（导出 `Tracer`）
- Test: `tests/test_tracer.py`

**Interfaces:**
- Consumes: `Trace`/`Span`（Task 1）、`TraceStore`（Task 1）。
- Produces: `class Tracer(store, now=time.time, id_factory=...)`：
  - `start_trace(session_id, user_input) -> contextmanager` yields `Trace`；退出时补 `ended_at/latency_ms/status` 并 `store.save_trace`
  - `span(name, kind) -> contextmanager` yields `Span`（无活动 trace 时 yield 一个游离 Span，不落库）
  - `on_event(event: dict) -> None`：`tool_call` 开 span、`tool_result` 关 span（成功与否解析 result JSON 的 `success`）
  - `current_trace() -> Optional[Trace]`

- [ ] **Step 1: 写失败测试**

`tests/test_tracer.py`：
```python
import json
import itertools

from app.observability.store import TraceStore
from app.observability.tracer import Tracer


def _tracer(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    clock = itertools.count(start=0, step=1)          # 确定性时钟：0,1,2,...
    ids = itertools.count(start=1)
    return Tracer(store, now=lambda: next(clock),
                  id_factory=lambda: f"id{next(ids)}"), store


def test_start_trace_persists_on_exit(tmp_path):
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "你好") as t:
        t.intent = "greeting"
    saved = store.get_trace(t.trace_id)
    assert saved is not None
    assert saved["status"] == "ok"
    assert saved["intent"] == "greeting"
    assert saved["latency_ms"] >= 0


def test_span_attached_to_trace(tmp_path):
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "hi") as t:
        with tracer.span("llm.chat.create", "llm") as sp:
            sp.prompt_tokens = 50
            sp.completion_tokens = 10
    saved = store.get_trace(t.trace_id)
    assert len(saved["spans"]) == 1
    assert saved["spans"][0]["kind"] == "llm"
    assert saved["prompt_tokens"] == 50


def test_on_event_pairs_tool_spans(tmp_path):
    tracer, store = _tracer(tmp_path)
    with tracer.start_trace("sess1", "查订单") as t:
        tracer.on_event({"type": "tool_call", "name": "query_order", "args": {"id": "A"}})
        tracer.on_event({"type": "tool_result",
                         "content": json.dumps({"success": True})})
    saved = store.get_trace(t.trace_id)
    tool_spans = [s for s in saved["spans"] if s["kind"] == "tool"]
    assert len(tool_spans) == 1
    assert tool_spans[0]["name"] == "tool:query_order"
    assert tool_spans[0]["success"] == 1


def test_error_status_recorded(tmp_path):
    tracer, store = _tracer(tmp_path)
    try:
        with tracer.start_trace("sess1", "boom") as t:
            raise RuntimeError("炸了")
    except RuntimeError:
        pass
    saved = store.get_trace(t.trace_id)
    assert saved["status"] == "error"
    assert "炸了" in saved["error"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_tracer.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.observability.tracer'`。

- [ ] **Step 3: 实现**

`app/observability/tracer.py`：
```python
"""Tracer：用 contextvar 维护当前 Trace，提供 span 与事件拦截。"""

import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from app.observability.trace import Trace, Span

_current: ContextVar[Optional[Trace]] = ContextVar("current_trace", default=None)


class Tracer:
    def __init__(self, store, now=time.time, id_factory=None):
        self.store = store
        self._now = now
        self._id = id_factory or (lambda: uuid.uuid4().hex[:16])
        self._pending_tool: list = []  # 已开、未关的工具 span 栈

    def current_trace(self) -> Optional[Trace]:
        return _current.get()

    @contextmanager
    def start_trace(self, session_id: str, user_input: str):
        start = self._now()
        trace = Trace(
            trace_id=self._id(), session_id=session_id, user_input=user_input,
            intent=None, started_at=start, ended_at=start, latency_ms=0.0,
            status="ok", error=None, spans=[],
        )
        token = _current.set(trace)
        try:
            yield trace
        except Exception as e:  # noqa: BLE001
            trace.status = "error"
            trace.error = str(e)
            raise
        finally:
            trace.ended_at = self._now()
            trace.latency_ms = (trace.ended_at - trace.started_at) * 1000.0
            _current.reset(token)
            try:
                self.store.save_trace(trace)
            except Exception:  # 落库失败不应影响主流程
                pass

    @contextmanager
    def span(self, name: str, kind: str):
        trace = _current.get()
        start = self._now()
        sp = Span(span_id=self._id(), trace_id=trace.trace_id if trace else "",
                  name=name, kind=kind, started_at=start, ended_at=start,
                  latency_ms=0.0)
        try:
            yield sp
        finally:
            sp.ended_at = self._now()
            sp.latency_ms = (sp.ended_at - sp.started_at) * 1000.0
            if trace is not None:
                trace.spans.append(sp)

    def on_event(self, event: dict) -> None:
        trace = _current.get()
        if trace is None:
            return
        etype = event.get("type")
        if etype == "tool_call":
            start = self._now()
            self._pending_tool.append(Span(
                span_id=self._id(), trace_id=trace.trace_id,
                name=f"tool:{event.get('name')}", kind="tool",
                started_at=start, ended_at=start, latency_ms=0.0,
                meta={"args": event.get("args", {})},
            ))
        elif etype == "tool_result" and self._pending_tool:
            sp = self._pending_tool.pop()
            sp.ended_at = self._now()
            sp.latency_ms = (sp.ended_at - sp.started_at) * 1000.0
            sp.success = _parse_success(event.get("content"))
            trace.spans.append(sp)


def _parse_success(content) -> Optional[bool]:
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "success" in data:
            return bool(data["success"])
    except Exception:
        pass
    return None
```
在 `app/observability/__init__.py` 顶部加入并补进 `__all__`：
```python
from app.observability.tracer import Tracer
```
（`__all__` 追加 `"Tracer"`）

- [ ] **Step 4: 运行确认通过**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_tracer.py -v`
Expected: PASS（4 passed）。

- [ ] **Step 5: 提交**

```bash
git add app/observability/tracer.py app/observability/__init__.py tests/test_tracer.py
git commit -m "feat(obs): Tracer（contextvar + span + 工具事件配对）"
```

---

## Task 3: TracingClient 透明代理（采集 LLM token/延迟）

**Files:**
- Create: `app/observability/client_proxy.py`
- Test: `tests/test_client_proxy.py`

**Interfaces:**
- Consumes: `Tracer`（Task 2）。
- Produces: `class TracingClient(real_client, tracer)`：透明代理，拦截 `.chat.completions.create` 与 `.beta.chat.completions.parse`，各记一个 `llm` span 并把 `response.usage` 写入 span；其余属性透传给 `real_client`。

- [ ] **Step 1: 写失败测试**

`tests/test_client_proxy.py`：
```python
import itertools

from app.observability.store import TraceStore
from app.observability.tracer import Tracer
from app.observability.client_proxy import TracingClient


class _Usage:
    def __init__(self, p, c):
        self.prompt_tokens = p
        self.completion_tokens = c


class _Resp:
    def __init__(self, p, c):
        self.usage = _Usage(p, c)


class _FakeCompletions:
    def create(self, **kw):
        return _Resp(120, 30)


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()
        self.other_attr = "passthrough"


def _tracer(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    clock = itertools.count()
    ids = itertools.count(1)
    return Tracer(store, now=lambda: next(clock), id_factory=lambda: f"id{next(ids)}"), store


def test_create_records_llm_span_with_tokens(tmp_path):
    tracer, store = _tracer(tmp_path)
    client = TracingClient(_FakeClient(), tracer)
    with tracer.start_trace("s", "hi") as t:
        resp = client.chat.completions.create(model="x", messages=[])
        assert resp.usage.prompt_tokens == 120
    saved = store.get_trace(t.trace_id)
    llm = [s for s in saved["spans"] if s["kind"] == "llm"]
    assert len(llm) == 1
    assert saved["prompt_tokens"] == 120
    assert saved["completion_tokens"] == 30


def test_passthrough_other_attributes(tmp_path):
    tracer, _ = _tracer(tmp_path)
    client = TracingClient(_FakeClient(), tracer)
    assert client.other_attr == "passthrough"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_client_proxy.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.observability.client_proxy'`。

- [ ] **Step 3: 实现**

`app/observability/client_proxy.py`：
```python
"""透明代理：包装 OpenAI 客户端，为 LLM 调用埋 span 并采集 token。

不改核心 chat.py —— 服务层临时把 agent.client 换成本代理即可。
"""


def _record_usage(span, resp) -> None:
    usage = getattr(resp, "usage", None)
    if usage is not None:
        span.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        span.completion_tokens = getattr(usage, "completion_tokens", 0) or 0


class _TracedCompletions:
    def __init__(self, real, tracer, span_name):
        self._real = real
        self._tracer = tracer
        self._span_name = span_name

    def create(self, **kwargs):
        with self._tracer.span(self._span_name, "llm") as sp:
            resp = self._real.create(**kwargs)
            _record_usage(sp, resp)
            return resp

    def parse(self, **kwargs):
        with self._tracer.span(self._span_name, "llm") as sp:
            resp = self._real.parse(**kwargs)
            _record_usage(sp, resp)
            return resp

    def __getattr__(self, name):
        return getattr(self._real, name)


class _TracedChat:
    def __init__(self, real, tracer, span_name):
        self._real = real
        self.completions = _TracedCompletions(real.completions, tracer, span_name)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _TracedBeta:
    def __init__(self, real, tracer):
        self._real = real
        self.chat = _TracedChat(real.chat, tracer, "llm.beta.parse")

    def __getattr__(self, name):
        return getattr(self._real, name)


class TracingClient:
    def __init__(self, real_client, tracer):
        self._real = real_client
        self.chat = _TracedChat(real_client.chat, tracer, "llm.chat.create")
        self.beta = _TracedBeta(real_client.beta, tracer)

    def __getattr__(self, name):
        return getattr(self._real, name)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_client_proxy.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```bash
git add app/observability/client_proxy.py tests/test_client_proxy.py
git commit -m "feat(obs): TracingClient 透明代理采集 LLM token/延迟"
```

---

## Task 4: 接入服务层（run_agent_streaming）

**Files:**
- Modify: `app/api/streaming.py`
- Test: `tests/test_streaming_trace.py`

**Interfaces:**
- Consumes: `Tracer`（Task 2）、`TracingClient`（Task 3）。
- Produces: `run_agent_streaming(agent, user_input, tracer=None, session_id="")` —— 新增两个默认参数；`tracer` 为空时行为与 W1 完全一致；非空时：worker 内 `with tracer.start_trace(session_id, user_input)`，`event_sink` 包一层调用 `tracer.on_event`，`agent.client` 临时替换为 `TracingClient`，`reply/metadata` 事件在 trace 上下文内产生，并把 `result.intent` 写入 `trace.intent`。

- [ ] **Step 1: 写失败测试**

`tests/test_streaming_trace.py`：
```python
import itertools

from app.api.streaming import run_agent_streaming
from app.observability.store import TraceStore
from app.observability.tracer import Tracer
from app.schemas.response import CustomerServiceResponse, IntentType


class _FakeClientPart:
    """让 agent.client.chat.completions.create 可被代理包装。"""
    class _C:
        class _Comp:
            def create(self, **kw): ...
        def __init__(self): self.completions = self._Comp()
    class _Beta:
        class _C2:
            class _Comp2:
                def parse(self, **kw): ...
            def __init__(self): self.completions = self._Comp2()
        def __init__(self): self.chat = self._C2()
    def __init__(self):
        self.chat = self._C()
        self.beta = self._Beta()


class FakeAgent:
    def __init__(self):
        self.event_sink = None
        self.client = _FakeClientPart()

    def chat(self, user_input):
        self.event_sink({"type": "tool_call", "name": "query_order", "args": {}})
        self.event_sink({"type": "tool_result", "content": '{"success": true}'})
        return CustomerServiceResponse(
            intent=IntentType.ORDER_QUERY, confidence=0.9,
            reply="已发货", requires_human=False, follow_up_question=None,
        )


def _tracer(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    clock = itertools.count()
    ids = itertools.count(1)
    return Tracer(store, now=lambda: next(clock), id_factory=lambda: f"id{next(ids)}"), store


def test_streaming_without_tracer_unchanged():
    # 不传 tracer，行为与 W1 一致：事件序列正常
    events = list(run_agent_streaming(FakeAgent(), "查订单"))
    assert [e["type"] for e in events] == \
        ["tool_call", "tool_result", "reply", "metadata", "done"]


def test_streaming_with_tracer_persists_trace(tmp_path):
    tracer, store = _tracer(tmp_path)
    events = list(run_agent_streaming(FakeAgent(), "查订单", tracer=tracer, session_id="sess1"))
    assert [e["type"] for e in events][-1] == "done"

    traces = store.recent_traces()
    assert len(traces) == 1
    assert traces[0]["intent"] == "order_query"
    assert traces[0]["status"] == "ok"

    detail = store.get_trace(traces[0]["trace_id"])
    tool_spans = [s for s in detail["spans"] if s["kind"] == "tool"]
    assert len(tool_spans) == 1
    assert tool_spans[0]["success"] == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_streaming_trace.py -v`
Expected: FAIL —— `run_agent_streaming() got an unexpected keyword argument 'tracer'`。

- [ ] **Step 3: 实现**

`app/api/streaming.py` 全文替换为：
```python
"""把阻塞式 agent.chat() 桥接成可被 SSE 消费的事件生成器（W2：可选接入 tracer）。"""

import queue
import threading
from typing import Iterator, Optional

_SENTINEL = object()


def run_agent_streaming(agent, user_input: str, tracer=None,
                        session_id: str = "") -> Iterator[dict]:
    """在后台线程运行 agent.chat()，把过程事件 + 最终结果按序 yield 出来。

    tracer 非空时：整次请求包一条 Trace，工具事件配对成 span，
    LLM 调用经 TracingClient 采集 token/延迟。tracer 为空时行为与 W1 一致。
    """
    q: "queue.Queue" = queue.Queue()

    def _run(sink):
        result = agent.chat(user_input)
        sink({"type": "reply", "content": result.reply})
        sink({
            "type": "metadata",
            "intent": result.intent.value,
            "confidence": result.confidence,
            "requires_human": result.requires_human,
            "follow_up_question": result.follow_up_question,
        })
        return result

    def worker():
        if tracer is None:
            agent.event_sink = q.put
            try:
                _run(q.put)
            except Exception as e:  # noqa: BLE001
                q.put({"type": "error", "message": str(e)})
            finally:
                agent.event_sink = None
                q.put(_SENTINEL)
            return

        # 接入 tracer 分支
        from app.observability.client_proxy import TracingClient

        def sink(ev):
            tracer.on_event(ev)
            q.put(ev)

        real_client = getattr(agent, "client", None)
        with tracer.start_trace(session_id, user_input) as trace:
            agent.event_sink = sink
            if real_client is not None:
                agent.client = TracingClient(real_client, tracer)
            try:
                result = _run(sink)
                trace.intent = result.intent.value
            except Exception as e:  # noqa: BLE001
                q.put({"type": "error", "message": str(e)})
                raise
            finally:
                agent.event_sink = None
                if real_client is not None:
                    agent.client = real_client
                q.put(_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        event = q.get()
        if event is _SENTINEL:
            yield {"type": "done"}
            return
        yield event
```
> 注：`raise` 让 `start_trace` 捕获异常并标记 `status="error"`；`_SENTINEL` 在 finally 里入队，保证消费端总能收到 `done`。

- [ ] **Step 4: 运行确认通过**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_streaming_trace.py tests/test_streaming.py -v`
Expected: PASS（W2 的 2 条 + W1 的 2 条都过，共 4 passed）。

- [ ] **Step 5: 提交**

```bash
git add app/api/streaming.py tests/test_streaming_trace.py
git commit -m "feat(obs): run_agent_streaming 接入 tracer（可选，W1 行为不变）"
```

---

## Task 5: 指标聚合 + API 端点

**Files:**
- Create: `app/observability/metrics.py`
- Modify: `app/api/app.py`
- Test: `tests/test_metrics.py`、`tests/test_dashboard_api.py`

**Interfaces:**
- Consumes: `TraceStore`（Task 1）。
- Produces:
  - `metrics.py`：`compute_metrics(store) -> dict`，含 `total_traces`、`error_rate`、`latency_p50_ms`、`latency_p95_ms`、`total_prompt_tokens`、`total_completion_tokens`、`est_cost_usd`、`tool_success_rate`、`tool_calls`、`intent_distribution`。
  - `app/api/app.py`：装配 `TraceStore`/`Tracer` 并传给 `run_agent_streaming`；新增 `GET /api/metrics`、`GET /api/traces`、`GET /api/traces/{trace_id}`。

- [ ] **Step 1: 写失败测试**

`tests/test_metrics.py`：
```python
from app.observability.store import TraceStore
from app.observability.trace import Trace, Span
from app.observability.metrics import compute_metrics


def _seed(store):
    store.save_trace(Trace(
        "t1", "s", "查订单", "order_query", 0.0, 0.2, 200.0, "ok", None,
        [Span("a", "t1", "llm.chat.create", "llm", 0, 0.1, 100, None, 100, 20, {}),
         Span("b", "t1", "tool:query_order", "tool", 0.1, 0.2, 100, True, 0, 0, {})],
    ))
    store.save_trace(Trace(
        "t2", "s", "退货", "return_request", 1.0, 1.6, 600.0, "error", "boom",
        [Span("c", "t2", "tool:apply_refund", "tool", 1.0, 1.1, 100, False, 0, 0, {})],
    ))


def test_compute_metrics(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    _seed(store)
    m = compute_metrics(store)
    assert m["total_traces"] == 2
    assert m["error_rate"] == 0.5
    assert m["tool_calls"] == 2
    assert m["tool_success_rate"] == 0.5           # 1 成功 / 2
    assert m["total_prompt_tokens"] == 100
    assert m["intent_distribution"]["order_query"] == 1
    assert m["latency_p50_ms"] >= 0
```

`tests/test_dashboard_api.py`：
```python
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.observability.store import TraceStore
from app.observability.trace import Trace, Span


def _client(tmp_path):
    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    store.save_trace(Trace(
        "t1", "s", "查订单", "order_query", 0.0, 0.2, 200.0, "ok", None,
        [Span("a", "t1", "tool:query_order", "tool", 0, 0.1, 100, True, 0, 0, {})],
    ))
    mgr = SessionManager(agent_factory=lambda p: None)
    app = create_app(session_manager=mgr, trace_store=store)
    return TestClient(app)


def test_metrics_endpoint(tmp_path):
    c = _client(tmp_path)
    r = c.get("/api/metrics")
    assert r.status_code == 200
    assert r.json()["total_traces"] == 1


def test_traces_endpoints(tmp_path):
    c = _client(tmp_path)
    lst = c.get("/api/traces").json()
    assert len(lst) == 1
    detail = c.get(f"/api/traces/{lst[0]['trace_id']}").json()
    assert detail["trace_id"] == "t1"
    assert len(detail["spans"]) == 1


def test_dashboard_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_metrics.py tests/test_dashboard_api.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.observability.metrics'`（及 `create_app` 不接受 `trace_store`）。

- [ ] **Step 3: 实现**

`app/observability/metrics.py`：
```python
"""基于 TraceStore 的指标聚合。"""

from app.config.settings import settings


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[k]


def compute_metrics(store) -> dict:
    traces = store.all_traces()
    spans = store.all_spans()

    total = len(traces)
    errors = sum(1 for t in traces if t["status"] == "error")
    latencies = [t["latency_ms"] for t in traces]
    prompt_tokens = sum(t["prompt_tokens"] or 0 for t in traces)
    completion_tokens = sum(t["completion_tokens"] or 0 for t in traces)

    tool_spans = [s for s in spans if s["kind"] == "tool"]
    tool_ok = sum(1 for s in tool_spans if s["success"] == 1)

    intent_dist: dict = {}
    for t in traces:
        key = t["intent"] or "unknown"
        intent_dist[key] = intent_dist.get(key, 0) + 1

    est_cost = (prompt_tokens / 1000.0) * settings.price_per_1k_prompt + \
               (completion_tokens / 1000.0) * settings.price_per_1k_completion

    return {
        "total_traces": total,
        "error_rate": (errors / total) if total else 0.0,
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "total_prompt_tokens": prompt_tokens,
        "total_completion_tokens": completion_tokens,
        "est_cost_usd": round(est_cost, 4),
        "tool_calls": len(tool_spans),
        "tool_success_rate": (tool_ok / len(tool_spans)) if tool_spans else 0.0,
        "intent_distribution": intent_dist,
    }
```
`app/api/app.py`：改造 `create_app` 装配 tracer + 新端点。在文件顶部 import 追加：
```python
from app.observability import TraceStore, Tracer
from app.observability.metrics import compute_metrics
```
把 `create_app` 签名与主体改为：
```python
def create_app(session_manager: Optional[SessionManager] = None,
               trace_store: Optional["TraceStore"] = None) -> FastAPI:
    app = FastAPI(title="Ecom Service Agent API")
    manager = session_manager or SessionManager()

    store = trace_store
    tracer = None
    if settings.obs_enabled or trace_store is not None:
        store = trace_store or TraceStore()
        store.init_schema()
        tracer = Tracer(store)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/chat")
    def chat(req: ChatRequest):
        agent = manager.get_or_create(req.session_id)
        lock = manager.get_lock(req.session_id)

        def event_stream():
            with lock:
                for event in run_agent_streaming(
                    agent, req.message, tracer=tracer, session_id=req.session_id
                ):
                    yield _sse_frame(event)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/session/reset")
    def reset(req: ResetRequest):
        manager.reset(req.session_id)
        return {"status": "reset"}

    @app.get("/api/metrics")
    def metrics():
        return compute_metrics(store)

    @app.get("/api/traces")
    def traces(limit: int = 20):
        return store.recent_traces(limit=limit)

    @app.get("/api/traces/{trace_id}")
    def trace_detail(trace_id: str):
        t = store.get_trace(trace_id)
        return t or {"error": "not found"}

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (_WEB_DIR / "chat.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        html = (_WEB_DIR / "dashboard.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    return app
```
> 需在文件顶部 `from app.config.settings import settings`（若尚未导入）。`settings` 之前未在 app.py 引入，请补上该 import。
> 注：`import` 里 `Optional` 已有；`TraceStore` 前向引用用字符串或直接类型均可。
> `web/dashboard.html` 由 Task 6 创建；若此刻尚未创建，先建占位 `<!doctype html><title>dashboard</title>` 以通过 `test_dashboard_page`。

- [ ] **Step 4: 运行确认通过**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_metrics.py tests/test_dashboard_api.py tests/test_api.py -v`
Expected: PASS（3 + 3 + 4 = 10 passed；`test_api.py` 回归确保 W1 端点未破）。

- [ ] **Step 5: 提交**

```bash
git add app/observability/metrics.py app/api/app.py tests/test_metrics.py tests/test_dashboard_api.py web/dashboard.html
git commit -m "feat(obs): 指标聚合 + /api/metrics /api/traces 端点 + dashboard 路由"
```

---

## Task 6: 看板页面 + 端到端

**Files:**
- Create/Replace: `web/dashboard.html`
- Modify: `README.md`
- Test: `test_dashboard_api.py::test_dashboard_page` 回归 + 人工端到端

**Interfaces:**
- Consumes: `GET /api/metrics`、`GET /api/traces`、`GET /api/traces/{id}`。

- [ ] **Step 1: 写页面**

`web/dashboard.html`（完整内容）：
```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>可观测性看板 · 小夕</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 960px; margin: 0 auto; padding: 16px; background:#f7f7f8; color:#222; }
  h1 { font-size: 20px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:20px; }
  .card { background:#fff; border:1px solid #e5e5e5; border-radius:10px; padding:14px; }
  .card .label { font-size:12px; color:#888; }
  .card .value { font-size:22px; font-weight:600; margin-top:4px; }
  table { width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid #eee; font-size:13px; }
  th { background:#fafafa; }
  .ok { color:#12864b; } .err { color:#c0392b; }
  button { padding:6px 12px; border:none; border-radius:8px; background:#4f7cff; color:#fff; cursor:pointer; }
  pre { background:#f0f0f2; padding:10px; border-radius:8px; overflow:auto; font-size:12px; }
</style>
</head>
<body>
  <h1>📊 可观测性看板 <button onclick="load()">刷新</button> <a href="/" style="float:right">← 回聊天</a></h1>
  <div class="cards" id="cards"></div>
  <h3>意图分布</h3>
  <div id="intents"></div>
  <h3>最近请求（点行看调用链）</h3>
  <table id="traces"><thead><tr><th>时间(相对)</th><th>意图</th><th>状态</th><th>延迟(ms)</th><th>tokens</th></tr></thead><tbody></tbody></table>
  <div id="detail"></div>
<script>
function card(label, value) {
  return `<div class="card"><div class="label">${label}</div><div class="value">${value}</div></div>`;
}
async function load() {
  const m = await (await fetch("/api/metrics")).json();
  document.getElementById("cards").innerHTML = [
    card("总请求数", m.total_traces),
    card("错误率", (m.error_rate*100).toFixed(1) + "%"),
    card("延迟 P50", m.latency_p50_ms.toFixed(0) + " ms"),
    card("延迟 P95", m.latency_p95_ms.toFixed(0) + " ms"),
    card("工具成功率", (m.tool_success_rate*100).toFixed(1) + "%"),
    card("工具调用数", m.tool_calls),
    card("Prompt tokens", m.total_prompt_tokens),
    card("Completion tokens", m.total_completion_tokens),
    card("估算成本", "$" + m.est_cost_usd),
  ].join("");
  document.getElementById("intents").innerHTML =
    Object.entries(m.intent_distribution).map(([k,v]) => `<span class="card" style="display:inline-block;margin:4px">${k}: <b>${v}</b></span>`).join("");

  const traces = await (await fetch("/api/traces?limit=30")).json();
  const tb = document.querySelector("#traces tbody");
  tb.innerHTML = traces.map(t => `
    <tr style="cursor:pointer" onclick="detail('${t.trace_id}')">
      <td>${t.started_at.toFixed(0)}</td>
      <td>${t.intent || "-"}</td>
      <td class="${t.status==='ok'?'ok':'err'}">${t.status}</td>
      <td>${t.latency_ms.toFixed(0)}</td>
      <td>${(t.prompt_tokens||0)}+${(t.completion_tokens||0)}</td>
    </tr>`).join("");
}
async function detail(id) {
  const t = await (await fetch("/api/traces/" + id)).json();
  const spans = (t.spans||[]).map(s =>
    `  [${s.kind}] ${s.name}  ${s.latency_ms.toFixed(0)}ms` +
    (s.success===null?"":(s.success?" ✅":" ❌")) +
    (s.prompt_tokens?`  tok:${s.prompt_tokens}+${s.completion_tokens}`:"")
  ).join("\n");
  document.getElementById("detail").innerHTML =
    `<h3>调用链 ${id}</h3><pre>用户: ${t.user_input}\n意图: ${t.intent} 状态: ${t.status}\n\n${spans}</pre>`;
}
load();
</script>
</body>
</html>
```

- [ ] **Step 2: 页面回归测试**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_dashboard_api.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 3: W2 全量测试**

Run: `cd "D:/2026项目/ecom-service-agent" && .venv/Scripts/python.exe -m pytest tests/test_trace_store.py tests/test_tracer.py tests/test_client_proxy.py tests/test_streaming_trace.py tests/test_metrics.py tests/test_dashboard_api.py -q`
Expected: 全绿（3+4+2+2+1+3 = 15 passed）。

- [ ] **Step 4: 端到端（需 API Key）**

```bash
.venv/Scripts/python.exe run_api.py
```
浏览器 `http://127.0.0.1:8010/` 聊几句（查订单、退货等），再打开 `http://127.0.0.1:8010/dashboard`：应看到请求数、延迟、token、工具成功率、意图分布，点某条请求能看到 LLM/工具调用链。

- [ ] **Step 5: 更新 README**

在 README「Web 服务」段落补：
```markdown
可观测性看板：浏览器打开 http://127.0.0.1:8010/dashboard
（展示延迟 P50/P95、token 成本、工具成功率、意图分布、每条请求的调用链）
```

- [ ] **Step 6: 提交**

```bash
git add web/dashboard.html README.md
git commit -m "feat(obs): 可观测性看板页 + README"
```

---

## Self-Review（作者自查）

- **Spec 覆盖**：对应设计文档第 2 节②可观测性、第 5 节 W2「Trace/Span 埋点 + SQLite 持久化 + 看板页面」。全部覆盖。✅
- **不动核心**：埋点仅在 `app/observability/` 与 `app/api/streaming.py`、`app/api/app.py`（服务层）；`chat.py`/`orchestrator.py` 零改动——用 event_sink 派生工具 span、用 TracingClient 代理采集 LLM token。✅
- **不破坏 W1**：`run_agent_streaming` 新增参数均有默认值，`test_streaming.py`（W1）与 `test_api.py`（W1）纳入回归。✅
- **占位符扫描**：无 TBD/TODO；`dashboard.html` 在 Task 5 先建占位、Task 6 换完整内容，已显式说明。✅
- **类型一致性**：`TraceStore` 方法（Task 1）/`Tracer`（Task 2）/`TracingClient`（Task 3）在服务层（Task 4）与端点（Task 5）调用签名一致；事件类型沿用 W1 的七种。✅

---

## 完成即达成的里程碑

聊几句后打开 `/dashboard`，能看到真实的延迟 P50/P95、token 成本、工具成功率、意图分布，并能点开任意一次请求查看完整 LLM/工具调用链——项目具备了"我怎么知道 Agent 线上表现好不好"的标准答案，也为 W3 护栏事件、W4 评估回归的 Trace 回流提供了数据底座。
```
