# W1：服务化 + 流式对话 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前 CLI-only 的电商客服 Agent 包装成一个 FastAPI 服务，通过 SSE 把 Agent 的 ReAct 过程（思考 / 调用工具 / 观察 / 最终回复）实时流式推送到一个极简 Web 聊天页，本地即可跑、可用、可测。

**Architecture:** 遵循「不动核心，只做加法」。核心 `EcomAgent` 现在把 ReAct 过程用 `_print_*` 打到控制台；本计划把这三个打印点收敛成一个可替换的**事件发射器 `event_sink`**（默认仍打印到控制台，CLI 行为完全不变）。服务层用「后台线程跑阻塞式 `chat()` + `queue.Queue` 把事件桥接成生成器」的经典模式，FastAPI 用 `StreamingResponse` 输出 SSE。前端用原生 JS 读流并渲染。

**Tech Stack:** Python 3.11+、FastAPI、uvicorn、SSE（text/event-stream）、原生 HTML/JS、pytest + httpx(TestClient)。

## Global Constraints

- **Python 3.11+**（项目现有约定）。
- **不破坏 CLI**：`python main.py` 的控制台输出、命令（reset/memory/skills/quit）必须与改造前完全一致。
- **不改核心 Agent 逻辑**：`EcomAgent` / `MultiAgentOrchestrator` 的 ReAct 循环、工具调用、结构化提取逻辑不动，只允许把 `_print_*` 收敛为 `_emit`。
- **测试离线可跑**：新增代码的单元测试**不得**依赖 `OPENAI_API_KEY` 或真实网络；对 Agent 一律用 stub/fake 注入。
- **Windows 友好**：路径用 `pathlib`，不用 POSIX-only 写法。
- **本阶段范围只覆盖单 Agent 模式**（`MULTI_AGENT_ENABLED=false`，项目默认）。Multi-Agent 的流式在后续周处理，本计划不做。
- **事件类型固定为**：`thought` / `tool_call` / `tool_result` / `reply` / `metadata` / `done` / `error`。

---

## File Structure

- `app/agent/chat.py` — 修改：`_print_*` → `_emit`，新增可替换 `event_sink` 属性。
- `app/api/__init__.py` — 新建：空包标记。
- `app/api/streaming.py` — 新建：把阻塞式 `agent.chat()` 桥接成事件生成器。
- `app/api/session_manager.py` — 新建：按 `session_id` 管理多个 Agent 实例 + 每会话锁。
- `app/api/schemas.py` — 新建：请求体 Pydantic 模型。
- `app/api/app.py` — 新建：`create_app()` 工厂 + 路由（/api/chat SSE、/api/session/reset、/api/health、/ 静态页）。
- `web/chat.html` — 新建：极简流式聊天前端。
- `run_api.py` — 新建：本地启动入口（`python run_api.py`）。
- `requirements.txt` — 修改：加 fastapi / uvicorn / httpx。
- `tests/test_streaming.py` — 新建：streaming 桥接单测。
- `tests/test_session_manager.py` — 新建：会话管理单测。
- `tests/test_api.py` — 新建：API 端点单测（TestClient + fake agent）。
- `tests/test_emit.py` — 新建：核心 `_emit` 行为单测。

---

## Task 1: 核心 Agent 事件发射器（`_emit`）

把 `EcomAgent` 的三个 `_print_*` 收敛成一个 `_emit(event: dict)`，新增可替换的 `event_sink`。默认 sink 保持原样打印到控制台，保证 CLI 零回归。

**Files:**
- Modify: `app/agent/chat.py`（`__init__` 尾部、`_react_loop` 内的三处调用、替换 `_print_thought/_print_action/_print_observation`）
- Test: `tests/test_emit.py`

**Interfaces:**
- Consumes: 无（核心内部改造）。
- Produces:
  - `EcomAgent.event_sink: Optional[Callable[[dict], None]]`（默认 `None`，可在实例创建后赋值）
  - `EcomAgent._emit(event: dict) -> None`
  - 事件字典结构：
    - `{"type": "thought", "content": str}`
    - `{"type": "tool_call", "name": str, "args": dict}`
    - `{"type": "tool_result", "content": str}`

- [ ] **Step 1: 写失败测试**

`tests/test_emit.py`：
```python
from app.agent.chat import EcomAgent


def _make_agent():
    # 构造不触发任何网络调用（OpenAI() 仅存配置；Memory/Skill 仅读本地文件）
    return EcomAgent(session_path="app/sessions/_test_emit.json")


def test_emit_routes_to_custom_sink():
    agent = _make_agent()
    captured = []
    agent.event_sink = captured.append

    agent._emit({"type": "thought", "content": "我在想"})
    agent._emit({"type": "tool_call", "name": "get_order", "args": {"id": "A1"}})
    agent._emit({"type": "tool_result", "content": "结果"})

    assert captured == [
        {"type": "thought", "content": "我在想"},
        {"type": "tool_call", "name": "get_order", "args": {"id": "A1"}},
        {"type": "tool_result", "content": "结果"},
    ]


def test_emit_default_prints_to_console(capsys):
    agent = _make_agent()  # event_sink 默认为 None
    agent._emit({"type": "thought", "content": "思考内容"})
    agent._emit({"type": "tool_call", "name": "get_order", "args": {"id": "A1"}})
    agent._emit({"type": "tool_result", "content": "x" * 400})

    out = capsys.readouterr().out
    assert "💭 [思考] 思考内容" in out
    assert "🔧 [调用工具] get_order(id='A1')" in out
    assert "📋 [工具结果]" in out
    assert "..." in out  # 超过 300 字被截断
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_emit.py -v`
Expected: FAIL —— `AttributeError: 'EcomAgent' object has no attribute 'event_sink'`（或 `_emit` 未定义）。

- [ ] **Step 3: 实现最小改动**

在 `app/agent/chat.py` 的 `__init__` 末尾（`self.summary` 初始化附近）加入：
```python
        # 事件发射器：默认 None（走控制台打印）；服务层可替换为队列写入等
        self.event_sink: Optional[Callable[[dict], None]] = None
```
在文件顶部 import 补上 `Callable`：
```python
from typing import Callable, Optional
```
用下面的 `_emit` + `_render_to_console` **替换**原有的 `_print_thought / _print_action / _print_observation` 三个方法：
```python
    def _emit(self, event: dict) -> None:
        """发射一个过程事件。默认打印到控制台；有 event_sink 时交给 sink。"""
        if self.event_sink is not None:
            self.event_sink(event)
        else:
            self._render_to_console(event)

    def _render_to_console(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "thought":
            print(f"\n💭 [思考] {event['content']}")
        elif etype == "tool_call":
            args_str = ", ".join(f"{k}={v!r}" for k, v in event["args"].items())
            print(f"🔧 [调用工具] {event['name']}({args_str})")
        elif etype == "tool_result":
            result = event["content"]
            display = result if len(result) <= 300 else result[:300] + "..."
            print(f"📋 [工具结果] {display}")
```
再把 `_react_loop` 内三处调用改为发射事件：
- `self._print_thought(assistant_msg.content)` → `self._emit({"type": "thought", "content": assistant_msg.content})`
- `self._print_action(func_name, func_args)` → `self._emit({"type": "tool_call", "name": func_name, "args": func_args})`
- `self._print_observation(result_str)` → `self._emit({"type": "tool_result", "content": result_str})`

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_emit.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 回归 CLI 冒烟（人工）**

Run: `python main.py`，输入 `你好` 观察控制台仍能打印思考/工具/回复；输入 `quit` 退出。
Expected: 输出格式与改造前一致。

- [ ] **Step 6: 提交**

```bash
git add app/agent/chat.py tests/test_emit.py
git commit -m "feat(core): 将 ReAct 过程打印收敛为可替换的 event_sink"
```

---

## Task 2: 流式桥接（阻塞 chat → 事件生成器）

`agent.chat()` 是阻塞的，且通过 `event_sink` 回调发事件。用后台线程 + 队列把它变成一个可被 SSE 消费的生成器，并在结尾补 `reply` / `metadata` / `done`（或异常时 `error`）。

**Files:**
- Create: `app/api/__init__.py`（空文件）
- Create: `app/api/streaming.py`
- Test: `tests/test_streaming.py`

**Interfaces:**
- Consumes: 任意具备 `event_sink` 属性且 `chat(str) -> CustomerServiceResponse` 的对象（`EcomAgent`）。
- Produces:
  - `run_agent_streaming(agent, user_input: str) -> Iterator[dict]`
  - 追加事件：
    - `{"type": "reply", "content": str}`
    - `{"type": "metadata", "intent": str, "confidence": float, "requires_human": bool, "follow_up_question": Optional[str]}`
    - `{"type": "done"}`
    - `{"type": "error", "message": str}`

- [ ] **Step 1: 写失败测试**

`tests/test_streaming.py`：
```python
from app.api.streaming import run_agent_streaming
from app.schemas.response import CustomerServiceResponse, IntentType


class FakeAgent:
    """模拟 EcomAgent：chat 时通过 event_sink 发过程事件，返回结构化结果。"""
    def __init__(self):
        self.event_sink = None

    def chat(self, user_input):
        self.event_sink({"type": "thought", "content": "在查订单"})
        self.event_sink({"type": "tool_call", "name": "get_order", "args": {"id": "A1"}})
        self.event_sink({"type": "tool_result", "content": "已发货"})
        return CustomerServiceResponse(
            intent=IntentType.ORDER_QUERY, confidence=0.9,
            reply="您的订单已发货", requires_human=False, follow_up_question=None,
        )


def test_stream_yields_process_then_reply_then_metadata_then_done():
    events = list(run_agent_streaming(FakeAgent(), "我的订单呢"))
    types = [e["type"] for e in events]
    assert types == ["thought", "tool_call", "tool_result", "reply", "metadata", "done"]
    assert events[3] == {"type": "reply", "content": "您的订单已发货"}
    assert events[4]["intent"] == "order_query"
    assert events[4]["confidence"] == 0.9
    assert events[4]["requires_human"] is False


class BoomAgent:
    def __init__(self):
        self.event_sink = None

    def chat(self, user_input):
        raise RuntimeError("模型炸了")


def test_stream_emits_error_then_done_on_exception():
    events = list(run_agent_streaming(BoomAgent(), "hi"))
    types = [e["type"] for e in events]
    assert types == ["error", "done"]
    assert "模型炸了" in events[0]["message"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_streaming.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.api.streaming'`。

- [ ] **Step 3: 实现**

先建空文件 `app/api/__init__.py`。再写 `app/api/streaming.py`：
```python
"""把阻塞式 agent.chat() 桥接成可被 SSE 消费的事件生成器。"""

import queue
import threading
from typing import Iterator

_SENTINEL = object()


def run_agent_streaming(agent, user_input: str) -> Iterator[dict]:
    """在后台线程运行 agent.chat()，把过程事件 + 最终结果按序 yield 出来。

    agent 需具备可写属性 event_sink 和方法 chat(str) -> CustomerServiceResponse。
    """
    q: "queue.Queue" = queue.Queue()

    def worker():
        agent.event_sink = q.put
        try:
            result = agent.chat(user_input)
            q.put({"type": "reply", "content": result.reply})
            q.put({
                "type": "metadata",
                "intent": result.intent.value,
                "confidence": result.confidence,
                "requires_human": result.requires_human,
                "follow_up_question": result.follow_up_question,
            })
        except Exception as e:  # noqa: BLE001 —— 流式场景需把异常透传给前端
            q.put({"type": "error", "message": str(e)})
        finally:
            agent.event_sink = None
            q.put(_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        event = q.get()
        if event is _SENTINEL:
            yield {"type": "done"}
            return
        yield event
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_streaming.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 5: 提交**

```bash
git add app/api/__init__.py app/api/streaming.py tests/test_streaming.py
git commit -m "feat(api): 阻塞式 chat 桥接为流式事件生成器"
```

---

## Task 3: 多会话管理器

按 `session_id` 隔离多个 Agent 实例（各自独立的会话持久化文件），并为每个会话加锁，避免同一会话并发请求踩状态。用工厂注入以便离线测试。

**Files:**
- Create: `app/api/session_manager.py`
- Test: `tests/test_session_manager.py`

**Interfaces:**
- Consumes: 一个 `agent_factory(session_path: str) -> agent`（默认创建 `EcomAgent`）。
- Produces:
  - `class SessionManager`
    - `__init__(self, agent_factory=None, base_dir="app/sessions/api")`
    - `get_or_create(session_id: str) -> agent`
    - `get_lock(session_id: str) -> threading.Lock`
    - `reset(session_id: str) -> None`

- [ ] **Step 1: 写失败测试**

`tests/test_session_manager.py`：
```python
import threading

from app.api.session_manager import SessionManager


class FakeAgent:
    def __init__(self, session_path):
        self.session_path = session_path
        self.reset_called = False

    def reset(self):
        self.reset_called = True


def _mgr():
    return SessionManager(agent_factory=lambda p: FakeAgent(p))


def test_same_session_returns_same_instance():
    mgr = _mgr()
    a = mgr.get_or_create("s1")
    b = mgr.get_or_create("s1")
    assert a is b


def test_different_sessions_are_isolated():
    mgr = _mgr()
    a = mgr.get_or_create("s1")
    b = mgr.get_or_create("s2")
    assert a is not b
    assert a.session_path != b.session_path


def test_session_path_uses_session_id():
    mgr = _mgr()
    a = mgr.get_or_create("abc")
    assert "abc" in a.session_path


def test_get_lock_is_stable_per_session():
    mgr = _mgr()
    l1 = mgr.get_lock("s1")
    l2 = mgr.get_lock("s1")
    assert l1 is l2
    assert isinstance(l1, type(threading.Lock()))


def test_reset_calls_agent_reset_and_drops_instance():
    mgr = _mgr()
    a = mgr.get_or_create("s1")
    mgr.reset("s1")
    assert a.reset_called is True
    # reset 后再取应是新实例
    assert mgr.get_or_create("s1") is not a
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_session_manager.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.api.session_manager'`。

- [ ] **Step 3: 实现**

`app/api/session_manager.py`：
```python
"""按 session_id 管理独立的 Agent 实例与会话锁。"""

import threading
from pathlib import Path


def _default_factory(session_path: str):
    from app.agent.chat import EcomAgent
    return EcomAgent(session_path=session_path)


class SessionManager:
    def __init__(self, agent_factory=None, base_dir: str = "app/sessions/api"):
        self._factory = agent_factory or _default_factory
        self._base_dir = Path(base_dir)
        self._agents: dict = {}
        self._locks: dict = {}
        self._guard = threading.Lock()  # 保护字典本身

    def _session_path(self, session_id: str) -> str:
        return str(self._base_dir / f"{session_id}.json")

    def get_or_create(self, session_id: str):
        with self._guard:
            if session_id not in self._agents:
                self._agents[session_id] = self._factory(self._session_path(session_id))
            return self._agents[session_id]

    def get_lock(self, session_id: str) -> threading.Lock:
        with self._guard:
            if session_id not in self._locks:
                self._locks[session_id] = threading.Lock()
            return self._locks[session_id]

    def reset(self, session_id: str) -> None:
        with self._guard:
            agent = self._agents.pop(session_id, None)
        if agent is not None and hasattr(agent, "reset"):
            agent.reset()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_session_manager.py -v`
Expected: PASS（5 passed）。

- [ ] **Step 5: 提交**

```bash
git add app/api/session_manager.py tests/test_session_manager.py
git commit -m "feat(api): 多会话 Agent 管理器 + 每会话锁"
```

---

## Task 4: FastAPI 应用与 SSE 端点

用 `create_app()` 工厂组装应用：`/api/chat`（SSE 流式）、`/api/session/reset`、`/api/health`、`/`（返回前端页）。工厂支持注入自定义 `SessionManager` 以便离线测试。

**Files:**
- Create: `app/api/schemas.py`
- Create: `app/api/app.py`
- Modify: `requirements.txt`（加 `fastapi>=0.110.0`、`uvicorn>=0.29.0`、`httpx>=0.27.0`）
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `run_agent_streaming`（Task 2）、`SessionManager`（Task 3）。
- Produces:
  - `app/api/schemas.py`：`ChatRequest(session_id: str, message: str)`、`ResetRequest(session_id: str)`
  - `app/api/app.py`：`create_app(session_manager: Optional[SessionManager] = None) -> FastAPI`
  - SSE 帧格式：每个事件一行 `data: <json>\n\n`。

- [ ] **Step 1: 安装依赖并写失败测试**

先改 `requirements.txt`，追加三行：
```
fastapi>=0.110.0
uvicorn>=0.29.0
httpx>=0.27.0
```
安装：
```bash
pip install "fastapi>=0.110.0" "uvicorn>=0.29.0" "httpx>=0.27.0"
```
`tests/test_api.py`：
```python
import json

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.session_manager import SessionManager
from app.schemas.response import CustomerServiceResponse, IntentType


class FakeAgent:
    def __init__(self, session_path):
        self.session_path = session_path
        self.event_sink = None
        self.reset_called = False

    def chat(self, user_input):
        self.event_sink({"type": "thought", "content": "思考中"})
        return CustomerServiceResponse(
            intent=IntentType.GREETING, confidence=0.8,
            reply=f"收到：{user_input}", requires_human=False, follow_up_question=None,
        )

    def reset(self):
        self.reset_called = True


def _client():
    mgr = SessionManager(agent_factory=lambda p: FakeAgent(p))
    return TestClient(create_app(session_manager=mgr)), mgr


def _parse_sse(text):
    return [json.loads(line[len("data: "):])
            for line in text.splitlines() if line.startswith("data: ")]


def test_health():
    client, _ = _client()
    assert client.get("/api/health").json() == {"status": "ok"}


def test_chat_streams_events():
    client, _ = _client()
    resp = client.post("/api/chat", json={"session_id": "s1", "message": "你好"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert types == ["thought", "reply", "metadata", "done"]
    assert events[1]["content"] == "收到：你好"


def test_reset_endpoint():
    client, mgr = _client()
    agent = mgr.get_or_create("s1")
    resp = client.post("/api/session/reset", json={"session_id": "s1"})
    assert resp.status_code == 200
    assert agent.reset_called is True


def test_root_serves_html():
    client, _ = _client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_api.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.api.app'`。

- [ ] **Step 3: 实现 schemas 与 app**

`app/api/schemas.py`：
```python
from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ResetRequest(BaseModel):
    session_id: str
```
`app/api/app.py`：
```python
"""FastAPI 应用工厂：SSE 流式对话 + 会话重置 + 静态前端。"""

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from app.api.schemas import ChatRequest, ResetRequest
from app.api.session_manager import SessionManager
from app.api.streaming import run_agent_streaming

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def _sse_frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(session_manager: Optional[SessionManager] = None) -> FastAPI:
    app = FastAPI(title="Ecom Service Agent API")
    manager = session_manager or SessionManager()

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/chat")
    def chat(req: ChatRequest):
        agent = manager.get_or_create(req.session_id)
        lock = manager.get_lock(req.session_id)

        def event_stream():
            with lock:  # 同一会话串行处理，避免并发踩状态
                for event in run_agent_streaming(agent, req.message):
                    yield _sse_frame(event)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/session/reset")
    def reset(req: ResetRequest):
        manager.reset(req.session_id)
        return {"status": "reset"}

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (_WEB_DIR / "chat.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    return app
```
> 注意：`test_root_serves_html` 依赖 `web/chat.html` 存在。若 Task 5 尚未完成，先建一个占位文件 `web/chat.html` 内容为 `<!doctype html><title>placeholder</title>`，Task 5 再替换为真实前端。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_api.py -v`
Expected: PASS（4 passed）。

- [ ] **Step 5: 提交**

```bash
git add app/api/schemas.py app/api/app.py requirements.txt tests/test_api.py web/chat.html
git commit -m "feat(api): FastAPI SSE 对话端点 + 会话重置 + 静态页"
```

---

## Task 5: 极简流式聊天前端

一个原生 HTML/JS 页面：输入框发消息 → 用 fetch 读 SSE 流 → 把 `thought/tool_call/tool_result` 渲染成可折叠的「Agent 思考过程」，把 `reply` 渲染成气泡，`metadata` 渲染成意图/置信度/转人工徽章。

**Files:**
- Create/Replace: `web/chat.html`（替换 Task 4 的占位文件）

**Interfaces:**
- Consumes: `POST /api/chat`（SSE）、`POST /api/session/reset`。
- Produces: 无（纯前端页面）。

- [ ] **Step 1: 写页面**

`web/chat.html`（完整内容）：
```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>智能客服「小夕」</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 720px; margin: 0 auto; padding: 16px; background:#f7f7f8; }
  h1 { font-size: 18px; }
  #log { display:flex; flex-direction:column; gap:12px; margin-bottom:16px; }
  .msg { padding:10px 14px; border-radius:12px; max-width:80%; white-space:pre-wrap; }
  .user { align-self:flex-end; background:#4f7cff; color:#fff; }
  .bot { align-self:flex-start; background:#fff; border:1px solid #e5e5e5; }
  .trace { align-self:flex-start; font-size:12px; color:#666; background:#f0f0f2; border-radius:8px; padding:6px 10px; max-width:80%; }
  .trace summary { cursor:pointer; }
  .badge { display:inline-block; font-size:12px; color:#555; margin-top:6px; }
  #bar { display:flex; gap:8px; position:sticky; bottom:0; background:#f7f7f8; padding:8px 0; }
  #inp { flex:1; padding:10px; border:1px solid #ccc; border-radius:8px; }
  button { padding:10px 16px; border:none; border-radius:8px; background:#4f7cff; color:#fff; cursor:pointer; }
  button:disabled { opacity:.5; }
</style>
</head>
<body>
  <h1>并夕夕 · 智能客服「小夕」<button id="reset" style="float:right;background:#888;">重置</button></h1>
  <div id="log"></div>
  <div id="bar">
    <input id="inp" placeholder="试试：我的订单还没发货，怎么回事？" autocomplete="off" />
    <button id="send">发送</button>
  </div>
<script>
const SESSION_ID = "web-" + Math.random().toString(36).slice(2, 10);
const log = document.getElementById("log");
const inp = document.getElementById("inp");
const sendBtn = document.getElementById("send");

function addMsg(text, cls) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.textContent = text;
  log.appendChild(d);
  window.scrollTo(0, document.body.scrollHeight);
  return d;
}

function newTrace() {
  const wrap = document.createElement("details");
  wrap.className = "trace"; wrap.open = true;
  const s = document.createElement("summary");
  s.textContent = "🧠 Agent 思考过程";
  wrap.appendChild(s);
  log.appendChild(wrap);
  return wrap;
}

function traceLine(trace, text) {
  const p = document.createElement("div");
  p.textContent = text;
  trace.appendChild(p);
  window.scrollTo(0, document.body.scrollHeight);
}

async function send() {
  const message = inp.value.trim();
  if (!message) return;
  inp.value = ""; sendBtn.disabled = true;
  addMsg(message, "user");
  const trace = newTrace();

  const resp = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: SESSION_ID, message }),
  });

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const frames = buf.split("\n\n");
    buf = frames.pop();
    for (const frame of frames) {
      if (!frame.startsWith("data: ")) continue;
      const ev = JSON.parse(frame.slice(6));
      if (ev.type === "thought") traceLine(trace, "💭 " + ev.content);
      else if (ev.type === "tool_call") traceLine(trace, "🔧 调用 " + ev.name + "(" + JSON.stringify(ev.args) + ")");
      else if (ev.type === "tool_result") traceLine(trace, "📋 " + ev.content.slice(0, 200));
      else if (ev.type === "reply") {
        trace.open = false;
        addMsg(ev.content, "bot");
      } else if (ev.type === "metadata") {
        const b = document.createElement("div");
        b.className = "badge";
        b.textContent = `[意图: ${ev.intent} | 置信度: ${(ev.confidence*100).toFixed(0)}% | 转人工: ${ev.requires_human ? "是" : "否"}]`;
        log.appendChild(b);
      } else if (ev.type === "error") {
        addMsg("⚠️ 出错了：" + ev.message, "bot");
      }
    }
  }
  sendBtn.disabled = false; inp.focus();
}

sendBtn.onclick = send;
inp.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
document.getElementById("reset").onclick = async () => {
  await fetch("/api/session/reset", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: SESSION_ID }),
  });
  log.innerHTML = "";
};
</script>
</body>
</html>
```

- [ ] **Step 2: 前端不做自动化测试（无逻辑可单测），仅确保 Task 4 的 `test_root_serves_html` 仍通过**

Run: `pytest tests/test_api.py::test_root_serves_html -v`
Expected: PASS。

- [ ] **Step 3: 提交**

```bash
git add web/chat.html
git commit -m "feat(web): 极简流式聊天前端（展示 ReAct 思考过程）"
```

---

## Task 6: 本地启动入口与端到端联调

提供 `python run_api.py` 一键启动，并做一次真实（需 API Key）的端到端人工联调，确认「本地能跑、能用、效果不错」。

**Files:**
- Create: `run_api.py`
- Test: 人工端到端（需 `.env` 配好可用的 `OPENAI_API_KEY` / `OPENAI_BASE_URL`）

**Interfaces:**
- Consumes: `create_app`（Task 4）。
- Produces: `python run_api.py` 在 `http://127.0.0.1:8000` 提供服务。

- [ ] **Step 1: 写启动入口**

`run_api.py`：
```python
"""本地启动 API 服务：python run_api.py"""

import uvicorn

from app.api.app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

- [ ] **Step 2: 全量单测回归**

Run: `pytest -q`
Expected: 全绿（含既有测试 + 本周新增 4 个测试文件）。

- [ ] **Step 3: 启动服务**

Run: `python run_api.py`
Expected: 控制台出现 `Uvicorn running on http://127.0.0.1:8000`。

- [ ] **Step 4: 端到端人工验证**

浏览器打开 `http://127.0.0.1:8000/`，依次输入：
- `我的订单还没发货，怎么回事？` → 观察「思考过程」里出现工具调用（查订单/物流），最终气泡给出回复 + 意图徽章。
- `有没有宽松透气的裤子推荐？` → 触发商品推荐。
- 点「重置」→ 对话清空。
Expected: 全链路流式可见、回复合理、徽章正确、重置生效。

- [ ] **Step 5: 更新 README 快速开始（加 Web 服务启动说明）**

在 `README.md` 的「快速开始」补一段：
```markdown
**启动 Web 服务（流式对话页）：**

​```bash
python run_api.py
# 浏览器打开 http://127.0.0.1:8000/
​```
```

- [ ] **Step 6: 提交**

```bash
git add run_api.py README.md
git commit -m "feat(api): 本地启动入口 run_api.py + README 服务说明"
```

---

## Self-Review（作者自查）

- **Spec 覆盖**：本计划对应设计文档第 5 节 W1「服务化（FastAPI + SSE 流式改造）+ 极简聊天前端」。护栏/可观测性/HITL/评估回归属 W2–W4，不在本计划范围。✅
- **占位符扫描**：无 TBD/TODO；每个代码步骤含完整代码。`web/chat.html` 在 Task 4 先建占位、Task 5 替换为完整内容——已显式说明，非占位符缺陷。✅
- **类型一致性**：`event_sink`（Task 1）在 Task 2/4 被读写一致；`run_agent_streaming`（Task 2）在 Task 4 被调用签名一致；`SessionManager.get_or_create/get_lock/reset`（Task 3）在 Task 4 使用一致；事件类型全程为固定七种。✅
- **范围**：单一子系统（服务化+流式），可独立产出「能跑的流式 Web 聊天」，适合一份计划。✅

---

## 完成即达成的里程碑

本地 `python run_api.py`，浏览器打开即可与 Agent **流式对话并看到完整 ReAct 思考过程**——项目从此不再是「CLI 教学 demo」，而是一个能演示的 Agent 服务。这是后续 W2（可观测性）埋点、W3（护栏/HITL）拦截、W4（评估回归）的承载底座。
```
