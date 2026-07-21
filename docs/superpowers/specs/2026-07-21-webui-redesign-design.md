# 前端美化（参考 nanobot）设计

- 日期：2026-07-21
- 分支：feature/w1-service-streaming
- 状态：已评审通过，待实现

## 1. 背景与目标

现有前端是单个 364 行手写 `web/chat.html`（原生 HTML/CSS/JS，FastAPI 以 HTMLResponse 直发），视觉简陋。
参考 [HKUDS/nanobot](https://github.com/HKUDS/nanobot) 的 WebUI（React 18 + Vite + TypeScript + Tailwind + shadcn/ui + lucide + react-markdown 的聊天工作台），把**聊天页**与**看板页**重建为现代、美观的界面。

设计原则：
- **后端零改动**：复用现有 SSE `/api/chat` 与全部 REST 端点，新 UI 只是更漂亮的消费端。
- **借布局、换品牌**：采用 nanobot 的布局/交互/组件质量，视觉身份换为「并夕夕·小夕」（电商蓝 primary + 暖色 accent）。
- **只做浅色主题**（不做深色切换）。
- **增量、可回退**：旧 `web/chat.html` 保留在 `/legacy`，坐席/评估继续走它。

非目标（YAGNI）：坐席/评估页重建、深色主题、i18n、任何后端改动。

## 2. 技术栈

React 18 + Vite + TypeScript + Tailwind CSS + shadcn/ui（Radix UI + class-variance-authority + clsx + tailwind-merge）+ lucide-react（图标）+ react-markdown（+ remark-gfm）。测试：Vitest + @testing-library/react。Node v25 / npm 11 已就绪。

## 3. 目录与构建

```
ecom-service-agent/
├── webui/                      # 新前端工程（独立 package.json）
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts          # build.outDir = ../web/dist；dev proxy /api → 127.0.0.1:8010
│   ├── tailwind.config.js
│   ├── tsconfig.json
│   └── src/
│       ├── main.tsx / App.tsx / globals.css
│       ├── components/ui/*      # shadcn 原语
│       ├── components/*         # 业务组件
│       ├── hooks/*
│       └── lib/*
└── web/
    ├── chat.html               # 旧页，保留
    └── dist/                    # 构建产物（gitignore 或提交，见 §8）
```

- **构建**：`cd webui && npm install && npm run build` → 输出到 `web/dist/`。
- **开发**：A 窗口 `python run_api.py`（:8010）；B 窗口 `cd webui && npm run dev`（:5173，Vite 代理 `/api` → `127.0.0.1:8010`）。

## 4. 后端改动（仅路由/托管，最小）

`app/api/app.py`：
- 若 `web/dist/index.html` 存在：`app.mount("/assets", StaticFiles(directory=web/dist/assets))`（或整体挂载），根路由 `/` 返回 `web/dist/index.html`；找不到 dist 时回退到旧 `chat.html`（保证未构建也能跑）。
- 新增 `/legacy`（和现 `/dashboard` 一样）返回旧 `web/chat.html`，供坐席/评估使用。
- `/api/*` 全部不变。

> 不改任何 Agent / 业务逻辑，只动前端托管的几行路由。

## 5. 复用的后端契约（不改）

- `POST /api/chat`（SSE）事件：`thought` / `guard{stage,action,reason}` / `tool_call{name,args}` / `tool_result{content}` / `reply{content}` / `metadata{intent,confidence,requires_human,follow_up_question}` / `handoff{reasons,handoff_id}` / `error{message}` / `done`。
- `POST /api/session/reset`、`GET /api/metrics`、`GET /api/traces[?limit&session_id]`、`GET /api/traces/{id}`、`POST /api/session/{id}/takeover`。
- session_id：沿用 localStorage key `xiaoxi_sid`；管理请求带 `X-Admin-Token`（localStorage `admin_token`，本地默认空）。

## 6. 布局与组件

### App 外壳
- 左 Sidebar：品牌头「并夕夕·小夕」；导航「💬 聊天 / 📊 看板」；「重置对话」；底部「更多（坐席/评估）」链接到 `/legacy`。
- 右主区：按导航渲染 ChatView / DashboardView。

### 聊天页（ChatView）
- `MessageList`：用户气泡（右，电商蓝实心）/ 小夕气泡（左，白卡 + 细边），小夕内容用 `react-markdown` 渲染。
- `AgentActivity`：每轮一个**可折叠 cluster**，按序展示 `thought`（💭）/`tool_call`（🔧 名+参数）/`tool_result`（📋 截断展示）/`guard`（🛡️）事件；收到 `reply` 后自动折叠。
- `MetadataChips`：回复下方彩色徽章 —— 意图 / 置信度 % / 转人工(是/否)。
- 转人工横幅：收到 `handoff` 事件显示「🎧 已转人工」提示条。
- `Composer`：多行输入 + 发送；Enter 发送、Shift+Enter 换行；流式中禁用；错误态友好提示。

### 看板页（DashboardView）
- `MetricCards`：卡片网格 —— 总请求 / 错误率 / 延迟 P50 / P95 / 工具成功率 / 工具调用数 / 护栏拦截 / 拦截率 / 脱敏数 / 转人工数 / 转人工率 / prompt tokens / completion tokens / 估算成本。
- `IntentBars`：意图分布（简单条形或 chips）。
- `TracesTable`：最近请求表（时间/会话/意图/状态/延迟/tokens），「只看本会话」开关；点行 → `TraceDetail`（Dialog/抽屉，展示 spans）。

### hooks / lib
- `useChatStream()`：POST `/api/chat`，流式读取并解析 SSE 帧为事件回调（含 buffer 拆帧逻辑，移植旧页 `split("\n\n")` 思路）。
- `useSession()`：localStorage `xiaoxi_sid`。
- `useMetrics()` / `useTraces()`：拉取看板数据。
- `lib/api.ts`：`adminFetch`（注入 `X-Admin-Token`）、`parseSSE`、`cn()`。

## 7. 主题与品牌

Tailwind + shadcn CSS 变量，**仅浅色**。primary = 电商蓝（≈ `#4f7cff`，与旧页一致）；accent = 暖色（用于议价/优惠强调）；中性灰阶背景。字体 system-ui。

## 8. 版本管理

`webui/node_modules` 与 `web/dist` 加入 `.gitignore`（源码入库、产物不入库）；README 补「前端构建/开发」说明。若希望「拉下来即可跑、无需 npm build」，可另行决定提交 `web/dist`（本设计默认不提交，构建产物由部署时生成）。

## 9. 测试

Vitest + Testing Library，覆盖关键件：
- `useChatStream` 的 SSE 分帧/事件解析（含跨 chunk 半帧拼接）。
- `MessageBubble`（Markdown 渲染）、`MetadataChips`（意图/置信度/转人工映射）、`AgentActivity`（各类事件渲染 + 折叠）。
- `MetricCards`（给定 metrics JSON 渲染卡片）。
后端 pytest 不动、保持全绿。

## 10. 落地策略与验收

增量交付、旧页全程可回退：
1. 脚手架 + 工具链 + 后端托管路由（能出一个空壳页 + `/legacy` 可用）。
2. App 外壳 + 聊天页（能正常对话、活动流、元信息、转人工）。
3. 看板页（指标卡 + 意图 + 请求表 + 调用链）。

验收标准：
1. `npm run build` 产物由 FastAPI 在 `/` 正常提供；未构建时 `/` 回退旧页不报错。
2. 聊天：发消息流式出思考/工具/回复，Markdown 正常，元信息徽章正确，触发转人工时有横幅；重置可用。
3. 看板：指标卡、意图分布、最近请求表、调用链详情均正确，「只看本会话」生效。
4. `/legacy` 仍可打开旧页并使用坐席/评估。
5. 关键前端单测通过；后端全量测试不回退。
6. 视觉为浅色、小夕品牌、nanobot 式布局（侧边栏 + 气泡 + 可折叠活动流）。
