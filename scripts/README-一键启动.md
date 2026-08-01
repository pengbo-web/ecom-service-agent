# 一键启动 · 使用说明

一条命令拉起「智能客服 agent + 真实黑马点评(hmdp)数据 + ApeRAG 知识库」全栈,
自动打开浏览器,**以预置 demo 身份(小鱼同学,已有 6 笔订单)零登录进入**,直接体验。

## 怎么用

- **启动**:双击项目根目录的 `一键启动.bat`,或命令行:
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
  ```
- **停止**:
  ```powershell
  powershell -File scripts\stop_all.ps1          # 停 agent/hmdp/mcp,保留 Redis/MySQL/ApeRAG
  powershell -File scripts\stop_all.ps1 -All      # 连 ApeRAG 容器一起停
  ```
- 启动后浏览器自动打开 **http://127.0.0.1:8010/**,进去就能问:
  - 我的订单有哪些
  - 帮我查下订单 ORD-20240115-001 的物流到哪了
  - 帮我看看某单能不能退款 / 砍砍价 / 帮我下单

## 启动做了什么(按依赖顺序,幂等,已在跑的自动跳过)

| 步 | 服务 | 端口 | 说明 |
|---|---|---|---|
| 1 | Redis | 6379 | 会话存储 + hmdp 身份反查(Docker,缺则自动拉起) |
| 2 | MySQL(heimadp) | 3306 | hmdp 业务库(校验;需已装好并有 heimadp) |
| 3 | hmdp Java 后端 | 8085 | 优先用预编译 `target\*.jar` 启动(`java -jar`),无 jar 回退 `mvn spring-boot:run` |
| 4 | hmdp-mcp | 9123 | 把 hmdp REST 包成 MCP 工具(`python -m mcp_server.hmdp_server`) |
| 5 | ApeRAG | 8100 | 知识库(`docker compose up -d`,后台拉起不阻塞) |
| 6 | 播 demo 身份 | — | 往 Redis 写 `login:token:{demo}`,令 demo token 成 hmdp 有效会话 |
| 7 | agent + SPA | 8010 | FastAPI + 智能客服前端(`run_api.py`) |

## demo 模式说明

由 `.env` 控制(`DEMO_MODE=true`):
- 前端启动读 `/api/config`,自动登录 `DEMO_HMDP_USER_ID`(=1),**跳过登录卡片**。
- 后端 `/api/chat` 自动注入 `DEMO_HMDP_TOKEN` → 解析成 hmdp userId=1 → 工具查**真实订单**。
- 关掉 demo:`.env` 里 `DEMO_MODE=false`,即恢复 agent 自有登录门。

## 依赖与前置(仅首次)

- Python venv:`.venv`(`pip install -r requirements.txt`)
- 前端产物:`web\dist`(`cd webui && npm install && npm run build`)
- JDK 8(hmdp 用)、Maven(仅在需要重新编译 hmdp jar 时)、Docker Desktop(Redis/ApeRAG)
- hmdp 代码在外部目录,jar 已预编译到其 `target\`;如换机器,改 `start_all.ps1` 顶部路径变量。

## 排障

- 日志都在 `.run\` 下:`agent.log(.err)`、`hmdp.log(.err)`、`hmdp-mcp.log(.err)`、`aperag.log`。
- agent 起不来先看 `.run\agent.log.err`;9123 未起看 `hmdp-mcp.log.err`。
- ApeRAG 冷启较慢(1-2 分钟),未就绪时 agent 自动降级本地知识索引(`KB_LOCAL_FALLBACK_ENABLED=true`),不影响订单类问答。
- Windows 上 `.ps1` 必须存为 **UTF-8 with BOM**,否则中文乱码导致解析失败。
