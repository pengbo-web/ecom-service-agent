# MCP 共享连接优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 消除"每会话 4 个 ToolManager(引擎+3 画像)各建一个 MCPClient、各连一次 9123"的重复连接开销——改为**进程级共享单个 MCPClient**(仿 session store 的 get_/set_ 单例模式),所有 ToolManager 复用同一连接与已发现的工具定义。

**Architecture:** 新增 `app/mcp_client/shared.py` 的进程级单例 `get_shared_mcp_client(url)`(带锁,首次 connect 并缓存 `(client, tools)`,失败返回 None)。`ToolManager._init_mcp` 从单例取,不再自建;标记 `_shared_mcp=True` 使 `close()` **不关闭共享 client**(否则一个会话结束会断掉别人正在用的连接)。白名单过滤仍各 ToolManager 本地做。并发前提:MCP `ClientSession` 支持并发 in-flight 请求(request-id 路由),多会话共享 `call_tool` 提交到同一后台 loop,由 `run_coroutine_threadsafe` 线程安全分发。

**Tech Stack:** 现有 mcp_client/MCPClient、ToolManager;标准库 threading。零新依赖。

## Global Constraints

- **进程级单例(逐字)**:`get_shared_mcp_client(server_url: str) -> tuple[MCPClient, list[dict]] | None`——首次按 url `connect()` 成功则缓存 `(client, tools)` 并返回;已缓存直接返回;`connect()` 抛异常返回 `None`(调用方降级)。`reset_shared_mcp()` 关闭并清空缓存(测试/重连用)。`threading.Lock` 保护首次创建(多 worker 并发只连一次)。
- **生命周期铁律**:共享 client **不随单个 ToolManager.close() 关闭**——`ToolManager` 标记 `self._shared_mcp: bool`;`close()` 仅当 `self._mcp_client and not self._shared_mcp` 才 close(本方案下 mcp 都是共享的,即从不由 ToolManager 关;由 `reset_shared_mcp()` 或进程退出关)。
- **降级不变**:`get_shared_mcp_client` 返回 None → `ToolManager._init_mcp` 走 `_init_local()`(现有语义,不崩)。
- **工具定义拷贝**:各 ToolManager 从单例拿到的 `tools` 要 `list(tools)` 拷贝后再合并本地工具/过滤,**不得就地改共享列表**。
- **白名单过滤仍本地**:`allowed_tools`(画像工具子集)在各 ToolManager 的 `_filter_tools` 做,不进单例。
- **并发正确性**:共享 client 的 `call_tool` 会被多会话/多线程并发调用——依赖 MCP `ClientSession` 的并发请求能力;若实测发现串扰,回退"每会话独立连接"(方案 Self-Review 已列此风险与回退)。
- 每任务:独立提交 + 指定测试文件绿(**切勿全量 pytest**);commit 末行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;推送 origin,绝不 upstream;`.venv/Scripts/python.exe`;测试不 print emoji。

## File Structure

| 文件 | 任务 | 责任 |
|---|---|---|
| `app/mcp_client/shared.py`(新) | S1 | `get_shared_mcp_client`/`reset_shared_mcp` 进程级单例 |
| `app/mcp_client/__init__.py`(改) | S1 | 导出上述 |
| `app/agent/tools/manager.py`(改) | S2 | `_init_mcp` 用单例;`close` 不关共享;`_shared_mcp` 标记 |
| `tests/test_mcp_shared.py`(新)、`tests/test_mcp_identity.py`(回归) | S1/S2 | 单例复用/降级/close 不关共享/过滤隔离 |

---

### Task S1: 共享单例 get_shared_mcp_client

**Files:**
- Create: `app/mcp_client/shared.py`
- Modify: `app/mcp_client/__init__.py`(导出)
- Test: `tests/test_mcp_shared.py`(新)

**Interfaces:**
- Consumes: `MCPClient(server_url)`、`MCPClient.connect() -> list[dict]`、`MCPClient.close()`。
- Produces: `get_shared_mcp_client(server_url: str) -> tuple[MCPClient, list[dict]] | None`;`reset_shared_mcp() -> None`;测试注入 `set_shared_mcp_for_test(client, tools)`。

- [ ] **Step 1: 写失败测试** `tests/test_mcp_shared.py`

```python
"""MCP 共享单例:一次连接全进程复用 + 失败降级 None + reset。全离线(fake MCPClient)。"""

import app.mcp_client.shared as shared


class _FakeMCP:
    instances = 0
    def __init__(self, url):
        _FakeMCP.instances += 1
        self.url = url; self.closed = False
    def connect(self):
        return [{"type": "function", "function": {"name": "query_order", "parameters": {}}}]
    def close(self):
        self.closed = True


class _FailMCP:
    def __init__(self, url): pass
    def connect(self): raise ConnectionError("server down")
    def close(self): pass


def setup_function():
    shared.reset_shared_mcp()
    _FakeMCP.instances = 0


def test_single_connect_reused_across_calls(monkeypatch):
    monkeypatch.setattr(shared, "MCPClient", _FakeMCP)
    a = shared.get_shared_mcp_client("http://x/mcp")
    b = shared.get_shared_mcp_client("http://x/mcp")
    assert a is not None and b is not None
    assert a[0] is b[0]                       # 同一 client 实例
    assert _FakeMCP.instances == 1            # 只连一次(4 个 ToolManager 复用)
    assert a[1][0]["function"]["name"] == "query_order"


def test_connect_failure_returns_none(monkeypatch):
    monkeypatch.setattr(shared, "MCPClient", _FailMCP)
    assert shared.get_shared_mcp_client("http://x/mcp") is None
    # 失败不缓存:下次仍尝试(可能 server 起来了)
    assert shared.get_shared_mcp_client("http://x/mcp") is None


def test_reset_closes_and_reconnects(monkeypatch):
    monkeypatch.setattr(shared, "MCPClient", _FakeMCP)
    a = shared.get_shared_mcp_client("http://x/mcp")
    client = a[0]
    shared.reset_shared_mcp()
    assert client.closed is True              # reset 关旧连接
    shared.get_shared_mcp_client("http://x/mcp")
    assert _FakeMCP.instances == 2            # reset 后重连
```

- [ ] **Step 2: 跑失败** → `.venv/Scripts/python.exe -m pytest tests/test_mcp_shared.py -q` FAIL

- [ ] **Step 3: 实现** `app/mcp_client/shared.py`

```python
"""进程级共享 MCP 连接:全进程一个 MCPClient,所有 ToolManager 复用。

原本每个 ToolManager(引擎+每个画像)各建一个 MCPClient+后台线程+连接,
一会话 4 次握手。共享后全进程一次。仿 session store 的 get_/set_ 单例模式。
"""

from __future__ import annotations

import threading

from app.mcp_client.client import MCPClient

_lock = threading.Lock()
_client: MCPClient | None = None
_tools: list | None = None
_url: str | None = None


def get_shared_mcp_client(server_url: str):
    """取全进程共享的 (client, tools);首次连接,失败返回 None(调用方降级)。"""
    global _client, _tools, _url
    with _lock:
        if _client is not None and _url == server_url:
            return _client, _tools
        try:
            c = MCPClient(server_url)
            tools = c.connect()
        except Exception:
            return None
        _client, _tools, _url = c, tools, server_url
        return _client, _tools


def reset_shared_mcp() -> None:
    """关闭并清空共享连接(测试/强制重连用)。"""
    global _client, _tools, _url
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = _tools = _url = None


def set_shared_mcp_for_test(client, tools) -> None:
    """测试注入(绕过真实连接)。"""
    global _client, _tools, _url
    _client, _tools, _url = client, tools, "test"
```

`app/mcp_client/__init__.py` 追加导出:

```python
from app.mcp_client.shared import get_shared_mcp_client, reset_shared_mcp  # noqa: F401
```

- [ ] **Step 4: 跑通过** → `pytest tests/test_mcp_shared.py -q` PASS
- [ ] **Step 5: 提交** `feat(mcp): S1 进程级共享 MCP 连接单例(get_shared_mcp_client)`

---

### Task S2: ToolManager 用共享连接 + close 不关共享

**Files:**
- Modify: `app/agent/tools/manager.py`(`__init__`/`_init_mcp`/`close`)
- Test: `tests/test_mcp_shared.py`(增)、`tests/test_mcp_identity.py`(回归)

**Interfaces:**
- Consumes: S1 的 `get_shared_mcp_client`。
- Produces: `ToolManager._shared_mcp: bool`;`_init_mcp` 从共享取;`close()` 不关共享 client。

- [ ] **Step 1: 写失败测试**(增到 `tests/test_mcp_shared.py`)

```python
def test_toolmanager_uses_shared_and_close_keeps_it(monkeypatch):
    import app.mcp_client.shared as sh
    from app.agent.tools.manager import ToolManager

    class _C:
        def __init__(self): self.closed = False
        def close(self): self.closed = True
        def call_tool(self, name, args): return "{}"
    fake = _C()
    tools = [{"type": "function", "function": {"name": "query_order", "parameters": {"type": "object", "properties": {}}}}]
    sh.set_shared_mcp_for_test(fake, tools)
    # 两个 ToolManager 都用同一个共享 client
    tm1 = ToolManager(use_mcp=True, mcp_server_url="test")
    tm2 = ToolManager(use_mcp=True, mcp_server_url="test")
    assert tm1._mcp_client is fake and tm2._mcp_client is fake
    assert tm1._shared_mcp is True
    tm1.close()                               # 一个会话结束
    assert fake.closed is False               # 共享 client 不被关(tm2 还在用)
    tm2.close()
    assert fake.closed is False               # 共享 client 始终由 reset/进程管
    sh.reset_shared_mcp()


def test_toolmanager_degrades_when_shared_none(monkeypatch):
    import app.mcp_client.shared as sh
    from app.agent.tools.manager import ToolManager
    monkeypatch.setattr(sh, "get_shared_mcp_client", lambda url: None)
    tm = ToolManager(use_mcp=True, mcp_server_url="test")
    names = {d["function"]["name"] for d in tm.tool_definitions}
    assert "list_user_orders" in names        # 降级本地工具(local 独有)
    assert tm._mcp_client is None
```

- [ ] **Step 2: 跑失败**

- [ ] **Step 3: 实现 manager.py**——`__init__` 加标记:

```python
        self._mcp_client = None
        self._shared_mcp = False
        self._tool_source: dict[str, str] = {}
        self._tool_defs: list[dict] = []
```

`_init_mcp` 改为从共享取(替换原 `MCPClient(...)` + `connect()`):

```python
    def _init_mcp(self, server_url: str):
        """从进程级共享连接取 MCP 工具;失败降级本地。"""
        from app.mcp_client import get_shared_mcp_client

        shared = get_shared_mcp_client(server_url)
        if shared is None:
            print(f"⚠️  [MCP] 连接失败,降级使用本地工具")
            self._init_local()
            return

        self._mcp_client, mcp_tools = shared
        self._shared_mcp = True

        mcp_names = set()
        for td in list(mcp_tools):            # 拷贝,不改共享列表
            name = td["function"]["name"]
            mcp_names.add(name)
            self._tool_source[name] = "mcp"
        self._tool_defs = list(mcp_tools)

        for td in LOCAL_TOOL_DEFINITIONS:
            name = td["function"]["name"]
            if name not in mcp_names:
                self._tool_defs.append(td)
                self._tool_source[name] = "local"
```

`close()` 改为不关共享:

```python
    def close(self):
        """清理:仅关闭本 ToolManager 独占的 MCP 连接;共享连接由 reset_shared_mcp/进程管。"""
        if self._mcp_client and not self._shared_mcp:
            self._mcp_client.close()
        self._mcp_client = None
```

- [ ] **Step 4: 跑通过 + 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_mcp_shared.py tests/test_mcp_identity.py tests/test_mcp.py tests/test_orchestrator_unified.py tests/test_controller_agent.py -q`
Expected: PASS(test_mcp 仍 skip;orchestrator/controller 构造多 ToolManager 现在复用共享——若这些测试未开 mcp 则走 local 不受影响)

- [ ] **Step 5: 提交** `feat(mcp): S2 ToolManager 复用共享 MCP 连接 + close 不关共享`

---

### Task S3: 端到端冒烟(控制方执行)

- [ ] 确保 MCP server(9123)常驻 + 主服务 MCP_ENABLED=true 重启
- [ ] 看启动日志:`🔗 [MCP] 已连接 ... 发现 5 个工具` **只出现 1 次**(此前每会话 4 次)——共享生效
- [ ] 登录用户连聊 2 轮(含查订单)→ 走 MCP 正常;确认订单归属校验仍生效(身份透传不受共享影响)
- [ ] 并发验证:两个用户(两个 token)几乎同时各发一条查订单 → 各自结果正确不串扰(共享 client 并发 call_tool)
- [ ] 停 MCP server 再发 → 降级本地(reset 后下次重连);对话不崩
- [ ] `.superpowers/sdd/progress.md` 记账

## 总量与顺序

S1(~0.4d)→ S2(~0.4d)→ S3(~0.2d),共 **~1 人日**。

## Self-Review

- **覆盖核对**:进程级单例只连一次(S1 test_single_connect_reused)✅;失败降级 None(S1)✅;reset 关旧重连(S1)✅;ToolManager 复用同一 client(S2)✅;close 不关共享(S2 test_..._close_keeps_it)✅;降级本地(S2)✅;白名单过滤仍本地(不进单例,allowed_tools 现逻辑不变)✅;身份透传不受影响(S2 回归 test_mcp_identity)✅;并发不串扰(S3 冒烟)✅。
- **占位符扫描**:无 TBD;S3 并发冒烟给了具体做法(两 token 同时查单)。
- **类型一致性**:`get_shared_mcp_client -> tuple|None`、`reset_shared_mcp`、`set_shared_mcp_for_test` S1 定义 S2/测试消费一致;`_shared_mcp: bool`、`_init_mcp`/`close` 改动自洽;`ctx_user_id` 身份透传(上个方案)与本次共享正交,不冲突(注入在 execute_tool、连接在 _init_mcp)。
- **已知取舍与风险**:①**并发**——共享 client 的 `call_tool` 多会话并发,依赖 MCP ClientSession 支持并发 in-flight 请求;S3 冒烟专门验并发不串扰,若发现串扰则回退"每会话独立连接"(改动可逆:_init_mcp 恢复自建)。②失败不缓存(每次重试)——server 晚起也能自动接上,代价是没起时每个 ToolManager 都试一次(4 次快速失败,可接受)。③共享 client 进程级不主动关(随进程退出)——daemon 后台线程,无泄漏风险。
