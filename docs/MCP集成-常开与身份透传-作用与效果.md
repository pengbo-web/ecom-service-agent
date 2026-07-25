# MCP 集成:常开 + 跨进程身份透传 + 共享连接 —— 作用与效果

> 本文记录把 MCP 从"实现了但默认关的演示能力"改造为"可一直开着的生产可用工具通道"的全过程:
> 常开并修复跨进程身份丢失、共享连接优化、集成测试规整。
> 提交范围:`11dbe54 · cf00672 · 5e8f9e8`(常开+身份透传)、`927c770 · 3c84d84 · e995d45 · 65220f3`(共享连接)、`80c758e`(测试规整),分支 `feature/w1-service-streaming`。

---

## 0. MCP 是什么、在本项目里的作用

**MCP(Model Context Protocol)** 是 Anthropic 提出的开放协议,让 Agent(host/client)以标准方式发现并调用外部工具(server)。本项目实现了**双端**:
- **Client**(`app/mcp_client/`):连 MCP server、发现工具、把 MCP 工具 schema 转成 OpenAI function 格式,合并进 `ToolManager`。
- **Server**(`mcp_server/server.py`,FastMCP):把电商工具经 `streamable-http` 暴露在 `127.0.0.1:9123/mcp`。

**作用**:提供一条**标准化的工具接入通道**——Agent 不必把工具都写成进程内 Python 函数,可以按协议调用远程/第三方工具。真实价值在"接异构后端与第三方工具"(订单系统、CRM、支付、知识库各是一个 MCP server,可独立部署)。本项目当前暴露的是自家 5 个工具(query_order/query_product/query_logistics/apply_refund/search_knowledge),属**能力展示 + 生产模式对齐**。

---

## 1. 改造前的状态与核心冲突

**改造前**:MCP 双端已实现,但 `mcp_enabled=False` 默认关——平时对话走进程内工具,MCP 从不启用。

**直接常开会踩的坑(本次改造的核心问题)**:MCP server 是**独立进程**,而项目的身份/会话/记忆防护都靠**同进程 ContextVar**(`chat()` 每轮 `set_current_user`/`set_current_session`/`set_memory_manager`)。MCP 把工具执行挪到另一个进程,ContextVar 传不过去:
- `query_order`/`query_logistics`/`apply_refund` 带 `owned_order` 归属校验,走 MCP 后 server 进程 `get_current_user()` 恒为 None
- **auth 开启** → 订单类工具全部 fail-closed(功能瘫痪);**auth 关闭** → 归属校验被绕过(跨用户越权复活)

**结论**:不能把 MCP 原样常开——必须先解决"身份跨进程传递"。

---

## 2. 三段改造

### 2.1 常开 + 跨进程身份透传(对齐生产 MCP 模式)

**做法**:身份靠**保留参数 `ctx_user_id`** 跨进程显式传递——这正是真实生产 MCP 的做法(生产用 OAuth/Bearer header,本项目用保留参数是等价务实简化)。

```
client 侧                                    server 侧(独立进程)
─────────────────────────                    ─────────────────────────
converter 从模型可见 schema                   每个 @mcp.tool() 加形参
剔除 ctx_user_id(模型无感)                    ctx_user_id,函数体第一行
       ↓                                       set_current_user(ctx_user_id)
ToolManager.execute_tool 的 mcp 分支                  ↓
注入 ctx_user_id = get_current_user()          owned_order 用它做归属校验
       ↓ (随 MCP 调用过去)  ─────────────────►  (跨进程身份落地)
```

| 提交 | 内容 |
|---|---|
| `11dbe54` | converter 过滤 `ctx_user_id`、ToolManager mcp 分支注入、server 5 工具接收并 `set_current_user`;`test_mcp.py` 环境依赖 skip 化 |
| `cf00672` | 冒烟抓到 FastMCP **禁止 `_` 开头的工具参数**(`InvalidSignature`,单测 mock schema 没触发、真起 server 才暴露)→ `_ctx_user_id` 改名 `ctx_user_id`;`.env` 开 `MCP_ENABLED`、`launch.json`/`docker-compose.yml` 加 mcp-server 常驻、ecom-agent 用服务名连 |
| `5e8f9e8` | 文档补身份透传一节 |

### 2.2 共享连接优化(每会话 4 连接 → 全进程 1)

**问题**:常开后每个会话建 orchestrator 时,引擎 + 售前/售中/售后 3 个画像的 `ToolManager` 各连一次 MCP(4 次握手/会话)。

**做法**:进程级共享单个 `MCPClient`(仿 session store 的 `get_/set_` 单例模式),所有 ToolManager 复用同一连接与已发现的工具定义。

| 提交 | 内容 |
|---|---|
| `927c770` | `app/mcp_client/shared.py`:`get_shared_mcp_client`(锁保护、首次连接缓存、失败返回 None 不缓存)+ `reset_shared_mcp` |
| `3c84d84` | `ToolManager` 从共享取;`_shared_mcp` 标记使 **`close()` 不关闭共享 client**(否则一会话结束会断掉别人正用的连接);连不上仍降级本地 |
| `e995d45` | 修评审抓的假阳性测试(degrade 用例 monkeypatch 没命中、靠 url 真连超时凑巧过 3s)→ 改走真实失败路径 |
| `65220f3` | 补回共享连接建立日志(全进程只打一次,恢复可观测性) |

### 2.3 集成测试规整(显式 opt-in)

`test_mcp.py` 是第 4 期老集成脚本,硬编码 4 工具(server 现 5)、本地期望 4(现 16)、`sys.exit` 非 assert、真 LLM e2e 脆慢、skipif 靠端口探测(MCP 常驻时误触发真跑失败)。

| 提交 | 内容 |
|---|---|
| `80c758e` | 重写为显式 opt-in(`RUN_MCP_INTEGRATION=1` 才跑,默认 skip 不干扰常规 pytest)+ 期望更新为 5 工具 + 断言 `ctx_user_id` 已被过滤 + assert 化 + 用无归属工具 `query_product` 避身份问题 + 删真 LLM e2e。连接/schema/降级逻辑由 hermetic 的 `test_mcp_shared`+`test_mcp_identity` 覆盖 |

---

## 3. 效果与证据(真服务冒烟)

- **常开工作**:启动日志 `🔗 [MCP] 已建立共享连接 ... 发现 5 个工具(全进程复用)`;5 个工具从 MCP 发现并合并进工具集。
- **身份透传生效**:大壮登录后查自己订单 `ORD-20240110-003` → 走 MCP → `success=True`(身份传过去了、server 端 `owned_order` 放行)。**没修的话 auth 开状态下这里会全拒**。
- **越权仍被拦**:`query_order`/`refund`/`cancel` 等越权(别人订单号)在 MCP 路径靠透传身份 + server 端 `owned_order` 拒绝(`test_mcp_identity` + `test_order_ownership` 单测覆盖)。
- **共享连接实锤**:MCP server 端日志 `Created new transport with session ID` **只出现一次**——一次会话的 4 个 ToolManager 复用了 1 个 session(共享前是 4 个)。
- **降级安全**:停掉 MCP server 再发消息 → `⚠️ [MCP] 连接失败,降级使用本地工具`,对话不崩。
- **测试**:`test_mcp_identity`(4)+ `test_mcp_shared`(5)hermetic 全绿;`test_mcp.py` 默认 5 skipped / opt-in + 真 server 5 passed。

---

## 4. 开关与部署

| 配置 | 默认 | 作用 |
|---|---|---|
| `mcp_enabled`(代码) / `MCP_ENABLED`(.env) | 代码 False / 本机 .env True | 是否启用 MCP;关闭走进程内工具 |
| `mcp_server_url` | `http://127.0.0.1:9123/mcp` | MCP server 地址(docker 内 ecom-agent 用 `mcp-server:9123` 覆盖) |

**server 常驻方式**:
- 本地:`.claude/launch.json` 的 `mcp-server` 配置,或 `python mcp_server/server.py`
- 生产:`docker-compose.yml` 的 `mcp-server` 服务(复用主镜像、共享 `app/sessions` 卷以访问同一 ecom.db、`depends_on` redis)

---

## 5. 真实生产 MCP 对照(可讲的深度)

| 生产特征 | 本项目 |
|---|---|
| 一个 host 连多个 MCP server,每个封装一个后端系统 | ⏳ 当前暴露自家工具(演示);接外部系统是后续方向 |
| **跨进程显式传身份**(OAuth/Bearer header 或调用参数) | ✅ 已做(`ctx_user_id` 保留参数,等价务实版;server 端独立校验) |
| host 侧治理:工具聚合/权限门控/审计/降级 | ✅ ToolManager 聚合 + 白名单(画像工具子集)+ Langfuse 追踪 + 降级 |
| 远程 streamable-http 传输、动态工具发现 | ✅ 已用 |
| 连接复用 | ✅ 进程级共享单例 |

---

## 6. 已知局限与后续

- **暴露自家工具**(演示性):生产价值在接第三方/异构后端 MCP server,本项目未接外部。
- **并发未压测**:共享 client 的 `call_tool` 多会话并发依赖 MCP session 的并发 in-flight 能力,仅单点冒烟验证,上量前值得压测;方案里保留了"若串扰则回退每会话独立连接"的可逆退路。
- **身份用保留参数而非 header**:生产更常用 OAuth/Bearer header;FastMCP 读 header 较重,保留参数是等价简化(已在 `docs/第4期-MCP集成.md` 标注生产替换路径)。

## 7. 面试叙事

"我实现了 MCP 双端,并把它从默认关的演示能力改造成可常开的生产通道。过程中最有价值的是发现**MCP 跨进程执行和'身份靠进程内上下文传递'这套设计的冲突**——直接常开会让订单越权防护失效。我用'保留参数透传身份'解决,这正是生产 MCP 的显式传身份模式;还做了连接复用(每会话 4 连接降到全进程 1)。冒烟里 FastMCP 拒绝下划线参数、评审里一个假阳性测试(靠超时凑巧过),都是只有真跑/查执行细节才暴露的问题。"
