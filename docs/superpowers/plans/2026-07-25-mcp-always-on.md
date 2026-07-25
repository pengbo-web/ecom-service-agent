# MCP 常开 + 跨进程身份透传 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 让 MCP 一直开着而不破坏订单归属校验——MCP 工具调用**透传当前用户身份**(client 注入 `_ctx_user_id`→server 端 `set_current_user` 再执行),使 `query_order`/`query_logistics`/`apply_refund` 走 MCP 远程进程时仍能做越权防护;对齐真实生产"MCP 显式传身份"模式。

**Architecture:** 身份靠**保留参数 `_ctx_user_id`** 跨进程传递:①`converter` 把该参数从模型可见的 schema 里剔除(模型无感、不会伪造);②`ToolManager` 执行 MCP 工具时用 `get_current_user()` 注入 `_ctx_user_id`;③MCP server 每个工具接收 `_ctx_user_id`、`set_current_user` 到 server 进程上下文后再调真实工具(工具内 `owned_order` 用它校验)。开启 `mcp_enabled`,server 用脚本/compose 常驻;冒烟验证跨进程越权拦截。

**Tech Stack:** 现有 mcp/mcp_server(FastMCP + streamable-http)、runtime_context、owned_order;标准库。零新依赖。

## Global Constraints

- **身份透传 = 保留参数 `_ctx_user_id`(逐字)**:client 侧不由模型填,由 `ToolManager` 在 MCP 分支注入 `get_current_user() or ""`;server 侧每个 `@mcp.tool()` 加形参 `_ctx_user_id: str = ""`,函数体**第一行** `set_current_user(_ctx_user_id or None)` 再调真实工具。
- **模型不可见 `_ctx_user_id`**:`converter.mcp_tools_to_openai` 必须从 `inputSchema.properties` 删除 `_ctx_user_id`、并从 `required` 移除——否则模型会看到并乱填。
- **server 端独立读同一 .env**:server 进程 `settings.auth_enabled` 与主服务一致(同 .env);`owned_order` 在 server 进程里靠透传的 `_ctx_user_id` 生效——**这是本方案的正确性核心**。
- **降级不变**:连不上 MCP 仍走 `_init_local()`(现有 try/except,不动);常开但 server 没起时行为=本地工具(不崩)。
- **只透传、不改工具业务逻辑**:`owned_order`/各工具函数体不改(它们已从 `get_current_user()` 读);只在 server wrapper 层 set 上下文。
- **无状态工具无害**:`query_product`/`search_knowledge` 也带 `_ctx_user_id` 形参(统一),但不使用它,行为不变。
- **常驻方式**:提供 `.claude/launch.json` 增 `mcp-server` 配置 + `docker-compose.yml` 增 `mcp-server` 服务(复用主镜像,command 换 `python mcp_server/server.py`);本地开发可 `python mcp_server/server.py` 手起。
- 每任务:独立提交 + 指定测试文件绿(**切勿全量 pytest**);commit 末行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;推送 origin,绝不 upstream;`.venv/Scripts/python.exe`;测试不 print emoji。
- **`tests/test_mcp.py` 环境依赖**:该文件 7 个测试需真 server(:9123),整分支终审时因 server 未起而 fail——本方案顺带把它们改成"server 未起则 skip"(不再 fail),纳入 P1。

## File Structure

| 文件 | 任务 | 责任 |
|---|---|---|
| `app/mcp_client/converter.py`(改) | P1 | 从模型可见 schema 剔除 `_ctx_user_id` |
| `app/agent/tools/manager.py`(改) | P1 | MCP 分支注入 `_ctx_user_id=get_current_user()` |
| `mcp_server/server.py`(改) | P1 | 5 工具加 `_ctx_user_id` 形参 + set_current_user |
| `tests/test_mcp_identity.py`(新)、`tests/test_mcp.py`(改 skip) | P1 | 透传单测 + 环境依赖 skip |
| `app/config/settings.py` / `.env` / `.claude/launch.json` / `docker-compose.yml`(改) | P2 | 开开关 + server 常驻 |

---

### Task P1: 身份透传三处 + 测试 skip 化

**Files:**
- Modify: `app/mcp_client/converter.py`、`app/agent/tools/manager.py`、`mcp_server/server.py`、`tests/test_mcp.py`
- Test: `tests/test_mcp_identity.py`(新)

**Interfaces:**
- Consumes: `runtime_context.get_current_user`/`set_current_user`;`MCPClient.call_tool(name, arguments)`;`owned_order`(server 端工具内已用)。
- Produces: converter 过滤后的 schema(无 `_ctx_user_id`);`ToolManager.execute_tool` 对 mcp 工具注入身份;server 工具接收身份。

- [ ] **Step 1: 写失败测试** `tests/test_mcp_identity.py`

```python
"""MCP 身份透传:converter 过滤 + ToolManager 注入 + server wrapper set 上下文。全离线。"""

from app.mcp_client.converter import mcp_tools_to_openai


class _FakeTool:
    def __init__(self, name, desc, schema):
        self.name = name; self.description = desc; self.inputSchema = schema


def test_converter_strips_ctx_user_id():
    schema = {"type": "object",
              "properties": {"order_id": {"type": "string"}, "_ctx_user_id": {"type": "string"}},
              "required": ["order_id", "_ctx_user_id"]}
    out = mcp_tools_to_openai([_FakeTool("query_order", "查单", schema)])
    props = out[0]["function"]["parameters"]["properties"]
    req = out[0]["function"]["parameters"].get("required", [])
    assert "_ctx_user_id" not in props        # 模型看不到
    assert "_ctx_user_id" not in req
    assert "order_id" in props                 # 业务参数保留


def test_toolmanager_injects_current_user_into_mcp_call(monkeypatch):
    from app.agent.tools.manager import ToolManager
    from app.agent.runtime_context import set_current_user

    tm = ToolManager.__new__(ToolManager)      # 绕过 __init__ 的真连接
    tm._tool_source = {"query_order": "mcp"}
    tm._OFFLOAD_EXEMPT = set()
    captured = {}

    class _FakeMcp:
        def call_tool(self, name, arguments):
            captured["name"] = name; captured["args"] = dict(arguments)
            return '{"success": true}'
    tm._mcp_client = _FakeMcp()
    monkeypatch.setattr(tm, "_maybe_offload", lambda n, r: r)

    set_current_user("alice")
    tm.execute_tool("query_order", {"order_id": "O1"})
    assert captured["args"]["order_id"] == "O1"
    assert captured["args"]["_ctx_user_id"] == "alice"     # 注入了当前用户
    set_current_user(None)


def test_toolmanager_injects_empty_when_no_user(monkeypatch):
    from app.agent.tools.manager import ToolManager
    from app.agent.runtime_context import set_current_user
    tm = ToolManager.__new__(ToolManager)
    tm._tool_source = {"query_order": "mcp"}; tm._OFFLOAD_EXEMPT = set()
    captured = {}
    class _FakeMcp:
        def call_tool(self, name, arguments): captured["args"] = dict(arguments); return "{}"
    tm._mcp_client = _FakeMcp()
    monkeypatch.setattr(tm, "_maybe_offload", lambda n, r: r)
    set_current_user(None)
    tm.execute_tool("query_order", {"order_id": "O1"})
    assert captured["args"]["_ctx_user_id"] == ""          # 无用户→空串(server 端 fail-closed)


def test_local_tools_not_injected(monkeypatch):
    """本地工具不注入 _ctx_user_id(它们直接读 ContextVar)。"""
    from app.agent.tools.manager import ToolManager
    tm = ToolManager.__new__(ToolManager)
    tm._tool_source = {"list_user_orders": "local"}; tm._OFFLOAD_EXEMPT = set()
    monkeypatch.setattr(tm, "_maybe_offload", lambda n, r: r)
    import app.agent.tools.manager as mod
    seen = {}
    monkeypatch.setattr(mod, "local_execute_tool", lambda n, a: seen.setdefault("args", dict(a)) or "{}")
    tm.execute_tool("list_user_orders", {})
    assert "_ctx_user_id" not in seen["args"]              # 本地不注入
```

- [ ] **Step 2: 跑失败** → `.venv/Scripts/python.exe -m pytest tests/test_mcp_identity.py -q` FAIL

- [ ] **Step 3: converter.py**——过滤 `_ctx_user_id`:

```python
def mcp_tools_to_openai(mcp_tools: list) -> list[dict]:
    """将 MCP Tool 列表转换为 OpenAI function calling 格式。

    剔除保留参数 _ctx_user_id(身份透传用,不该暴露给模型——否则模型会看到并伪造)。
    """
    result = []
    for tool in mcp_tools:
        schema = dict(tool.inputSchema or {})
        props = dict(schema.get("properties") or {})
        props.pop("_ctx_user_id", None)
        schema["properties"] = props
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r != "_ctx_user_id"]
        result.append({
            "type": "function",
            "function": {"name": tool.name, "description": tool.description or "",
                         "parameters": schema},
        })
    return result
```

- [ ] **Step 4: manager.py**——`execute_tool` 的 mcp 分支注入(顶部 import 惰性):

```python
    def execute_tool(self, name: str, arguments: dict) -> str:
        """分发调用;结果超长则落盘留指针(不丢信息),防单条结果撑爆上下文窗口。"""
        source = self._tool_source.get(name)

        if source == "mcp" and self._mcp_client:
            # 跨进程身份透传:MCP 工具在独立 server 进程执行,ContextVar 传不过去,
            # 用保留参数把当前用户带过去(server 端 set_current_user 后 owned_order 才能校验)。
            from app.agent.runtime_context import get_current_user
            args = {**arguments, "_ctx_user_id": get_current_user() or ""}
            result = self._mcp_client.call_tool(name, args)
        elif source == "local":
            result = local_execute_tool(name, arguments)
        else:
            result = json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

        return self._maybe_offload(name, result)
```

- [ ] **Step 5: mcp_server/server.py**——5 工具加 `_ctx_user_id` 形参 + set(顶部加 import):

```python
from app.agent.runtime_context import set_current_user
```

每个工具改造(以 query_order 为例,其余同构):

```python
@mcp.tool()
def query_order(order_id: str, _ctx_user_id: str = "") -> str:
    """根据订单号查询订单详情，包括订单状态、商品信息、金额、物流单号等"""
    set_current_user(_ctx_user_id or None)      # 跨进程身份落地,owned_order 据此校验
    result = _query_order(order_id)
    return json.dumps(result, ensure_ascii=False)
```

（query_product/query_logistics/apply_refund/search_knowledge 全部在函数体第一行加 `set_current_user(_ctx_user_id or None)` + 形参 `_ctx_user_id: str = ""`;product/search_knowledge 不使用它但保持统一。apply_refund 形参顺序 `(order_id, reason, _ctx_user_id="")`。）

- [ ] **Step 6: test_mcp.py 环境依赖 skip 化**——文件顶部加:

```python
import pytest, socket

def _server_up(host="127.0.0.1", port=9123):
    s = socket.socket(); s.settimeout(0.3)
    try:
        s.connect((host, port)); return True
    except OSError:
        return False
    finally:
        s.close()

pytestmark = pytest.mark.skipif(not _server_up(), reason="MCP server(:9123)未运行,跳过集成测试")
```

（这样 server 没起时这些集成测试 skip 而非 fail;起了则正常跑。）

- [ ] **Step 7: 跑通过** → `.venv/Scripts/python.exe -m pytest tests/test_mcp_identity.py tests/test_mcp.py tests/test_order_ownership.py -q`
Expected: identity 5 passed;test_mcp skipped(server 未起);ownership 不受影响

- [ ] **Step 8: 提交** `feat(mcp): P1 MCP 工具跨进程身份透传(_ctx_user_id)+ 测试 skip 化`

---

### Task P2: 开启常开 + server 常驻 + 冒烟接线

**Files:**
- Modify: `app/config/settings.py`(注释)、`.env`、`.claude/launch.json`、`docker-compose.yml`
- Test: 无新单测(配置);冒烟在 P3

**Interfaces:**
- Consumes: P1 的身份透传(开启后才安全)。
- Produces: `mcp_enabled=True` 运行;`mcp-server` 常驻入口(launch/compose)。

- [ ] **Step 1: settings 注释**——`mcp_enabled` 默认仍 `False`(代码默认保守),但注释说明"开启前提是 server 常驻 + 身份透传已就位(P1)":

```python
    mcp_enabled: bool = False   # 开启前需:①MCP server 常驻(mcp_server/server.py)②身份透传已实现(P1);否则订单类工具在 server 进程拿不到用户上下文
    mcp_server_url: str = "http://127.0.0.1:9123/mcp"
```

- [ ] **Step 2: `.env` 开启**——把 `MCP_ENABLED=false` 改为 `true`(本机常开):

```
MCP_ENABLED=true
```

- [ ] **Step 3: `.claude/launch.json` 增 mcp-server 配置**(与 ecom-agent 并列):

```json
    {
      "name": "mcp-server",
      "runtimeExecutable": "cmd",
      "runtimeArgs": ["/c", "cd /d D:\\2026项目\\ecom-service-agent && .venv\\Scripts\\python.exe mcp_server\\server.py"],
      "port": 9123
    }
```

- [ ] **Step 4: `docker-compose.yml` 增 mcp-server 服务**(复用 ecom-agent 镜像/构建,command 换;挂载与 ecom-agent 一致以共享 ecom.db):

```yaml
  mcp-server:
    build: .
    command: python mcp_server/server.py
    ports:
      - "9123:9123"
    volumes:
      - ./app/sessions:/app/app/sessions
    environment:
      - MCP_ENABLED=false   # server 自身不作为 MCP client,避免自连
    depends_on:
      - redis
```

（注:按 `docker-compose.yml` 里 ecom-agent 现有的 build/volumes/environment 实际写法对齐——实现者读现文件后照搬其挂载与环境变量风格;此处为结构示意,volumes 至少覆盖 ecom.db 所在目录。）

- [ ] **Step 5: 提交** `feat(mcp): P2 开启 MCP 常开 + server 常驻(launch/compose)`

---

### Task P3: 端到端冒烟(控制方执行)

- [ ] 起 MCP server:`.venv/Scripts/python.exe mcp_server/server.py`(常驻;确认监听 9123)
- [ ] 起主服务(MCP_ENABLED=true,auth 开)→ 启动日志出现 `🔗 [MCP] 已连接 ... 发现 5 个工具`
- [ ] 登录 `大壮`(有 1 单)→ 问"查订单 <大壮自己的订单号>" → 走 MCP(Langfuse/活动面板 tool_call 名来自 MCP)→ 返回订单(透传身份→server 端 owned_order 校验通过)
- [ ] 大壮问别人的订单号 → "未找到该订单"(跨进程越权仍被拦——本方案核心验收)
- [ ] 问商品/政策(query_product/search_knowledge 走 MCP)→ 正常(无状态工具)
- [ ] 停掉 MCP server 再发消息 → 主服务日志 `⚠️ [MCP] 连接失败,降级使用本地工具`,对话仍正常(降级验证)
- [ ] `list_user_orders`/`query_coupons`/`save_user_memory`(未上 MCP)→ 仍本地、归属/资格/记忆正常
- [ ] Langfuse:MCP 工具调用可见;确认订单归属在 MCP 路径生效
- [ ] `.superpowers/sdd/progress.md` 记账;更新 `docs/第4期-MCP集成.md` 补"身份透传"一节

## 总量与顺序

P1(~1d)→ P2(~0.3d)→ P3(~0.2d),共 **~1.5 人日**。P1 是正确性核心,P2/P3 依赖它。

## Self-Review

- **覆盖核对**:身份透传三处(converter 过滤/ToolManager 注入/server set)P1 各有测试 ✅;模型不可见 `_ctx_user_id`(converter 测试)✅;无用户→空串→server fail-closed ✅;本地工具不注入(不干扰现有 ContextVar 路径)✅;降级不变(P3 冒烟)✅;test_mcp skip 化(修终审那 5 个 fail)✅;开开关+常驻(P2)✅;跨进程越权拦截(P3 核心验收)✅。
- **占位符扫描**:P2 Step4 的 compose 片段标注"结构示意,实现者读现文件照搬挂载/环境风格"——给了明确依据(对齐 ecom-agent 现有写法),非 TBD;其余完整代码。
- **类型一致性**:`_ctx_user_id: str = ""` 在 server 形参、converter 过滤键、ToolManager 注入键三处字面一致;`get_current_user()/set_current_user()` 复用 runtime_context;`owned_order` 不改(server 进程内经 set_current_user 后自然生效)。
- **已知取舍**:①身份用保留参数传(生产更常用 OAuth/Bearer header,FastMCP 读 header 较重;保留参数是等价务实简化,文档标注)②MCP server 与主服务需访问同一 ecom.db(本地同文件/compose 同卷)③mcp_enabled 代码默认仍 False(保守),靠 .env 开启——避免没起 server 的环境默认踩降级开销。
