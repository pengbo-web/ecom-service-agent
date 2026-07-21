# 前端美化（参考 nanobot）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 React18 + Vite + TypeScript + Tailwind + shadcn/ui 重建「聊天页」与「看板页」，风格参考 nanobot（侧边栏 + 气泡 + 可折叠 Agent 活动流 + Markdown），浅色 + 小夕品牌；后端零业务改动，仅改前端托管路由。

**Architecture:** 新前端工程在 `webui/`，构建产物输出到 `web/dist/`（提交入库）。FastAPI 在 `/` 提供 SPA、`/legacy` 提供旧页（坐席/评估）、`/api/*` 不变。新 UI 复用现有 SSE `/api/chat` 与 REST 端点。

**Tech Stack:** React 18, Vite, TypeScript, Tailwind CSS, shadcn/ui（Radix + cva + clsx + tailwind-merge）, lucide-react, react-markdown + remark-gfm, Vitest + @testing-library/react。Node v25 / npm 11。

**Spec:** `docs/superpowers/specs/2026-07-21-webui-redesign-design.md`

**约定：** 前端命令都在 `webui/` 下执行（`cd /Users/shine/ecom-service-agent/webui`）。后端命令在仓库根、需 `source .venv/bin/activate`。

### 复用的后端契约（不要改后端业务）
- SSE `POST /api/chat` body `{session_id, message}`，事件 `data: {json}\n\n`，type ∈ `thought{content}` / `tool_call{name,args}` / `tool_result{content}` / `guard{stage,action,guard,reason}` / `reply{content}` / `metadata{intent,confidence,requires_human,follow_up_question}` / `handoff{reasons,handoff_id}` / `error{message}` / `done`。
- `POST /api/session/reset` `{session_id}`；`GET /api/metrics`；`GET /api/traces?limit=&session_id=`；`GET /api/traces/{id}`。
- localStorage：`xiaoxi_sid`（会话）、`admin_token`（管理请求头 `X-Admin-Token`，本地默认空）。
- metrics 字段：`total_traces,error_rate,latency_p50_ms,latency_p95_ms,tool_success_rate,tool_calls,guard_blocks,block_rate,guard_sanitizes,handoffs,escalation_rate,total_prompt_tokens,total_completion_tokens,est_cost_usd,intent_distribution`。

---

### Task 1: 脚手架 + Tailwind + 构建配置

**Files（均 Create）:** `webui/package.json`、`webui/index.html`、`webui/vite.config.ts`、`webui/tsconfig.json`、`webui/tsconfig.node.json`、`webui/postcss.config.js`、`webui/tailwind.config.js`、`webui/src/globals.css`、`webui/src/main.tsx`、`webui/src/App.tsx`（占位）、`webui/src/vite-env.d.ts`、`webui/src/test/setup.ts`、`webui/.gitignore`

- [ ] **Step 1: 建 `webui/package.json`**

```json
{
  "name": "ecom-webui",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview",
    "test": "vitest run"
  },
  "dependencies": {
    "class-variance-authority": "^0.7.1",
    "clsx": "^2.1.1",
    "lucide-react": "^0.469.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-markdown": "^9.0.1",
    "remark-gfm": "^4.0.0",
    "tailwind-merge": "^2.6.0",
    "@radix-ui/react-dialog": "^1.1.4",
    "@radix-ui/react-separator": "^1.1.1",
    "@radix-ui/react-slot": "^1.1.1",
    "@radix-ui/react-tooltip": "^1.1.6"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.6.3",
    "@testing-library/react": "^16.1.0",
    "@types/node": "^24.0.0",
    "@types/react": "^18.3.18",
    "@types/react-dom": "^18.3.5",
    "@vitejs/plugin-react": "^4.3.4",
    "autoprefixer": "^10.4.20",
    "happy-dom": "^16.3.0",
    "postcss": "^8.4.49",
    "tailwindcss": "^3.4.17",
    "typescript": "^5.6.3",
    "vite": "^6.0.5",
    "vitest": "^2.1.8"
  }
}
```

- [ ] **Step 2: 建 `webui/index.html`**

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>并夕夕 · 智能客服「小夕」</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 3: 建 `webui/vite.config.ts`**（产物到 `web/dist`、dev 代理、Vitest）

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  base: "/",
  build: { outDir: "../web/dist", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8010" },
  },
  test: {
    environment: "happy-dom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
```

- [ ] **Step 4: 建 `webui/tsconfig.json` 与 `webui/tsconfig.node.json`**

`webui/tsconfig.json`:
```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "baseUrl": ".",
    "paths": { "@/*": ["./src/*"] },
    "types": ["vitest/globals", "@testing-library/jest-dom"]
  },
  "include": ["src"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
```
`webui/tsconfig.node.json`:
```json
{
  "compilerOptions": {
    "composite": true,
    "skipLibCheck": true,
    "module": "ESNext",
    "moduleResolution": "bundler",
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "noEmit": true
  },
  "include": ["vite.config.ts"]
}
```

- [ ] **Step 5: 建 `webui/postcss.config.js` 与 `webui/tailwind.config.js`**

`webui/postcss.config.js`:
```js
export default { plugins: { tailwindcss: {}, autoprefixer: {} } };
```
`webui/tailwind.config.js`（shadcn 令牌，浅色）:
```js
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: { DEFAULT: "hsl(var(--primary))", foreground: "hsl(var(--primary-foreground))" },
        secondary: { DEFAULT: "hsl(var(--secondary))", foreground: "hsl(var(--secondary-foreground))" },
        muted: { DEFAULT: "hsl(var(--muted))", foreground: "hsl(var(--muted-foreground))" },
        accent: { DEFAULT: "hsl(var(--accent))", foreground: "hsl(var(--accent-foreground))" },
        card: { DEFAULT: "hsl(var(--card))", foreground: "hsl(var(--card-foreground))" },
        destructive: { DEFAULT: "hsl(var(--destructive))", foreground: "hsl(var(--destructive-foreground))" },
      },
      borderRadius: { lg: "var(--radius)", md: "calc(var(--radius) - 2px)", sm: "calc(var(--radius) - 4px)" },
    },
  },
  plugins: [],
};
```

- [ ] **Step 6: 建 `webui/src/globals.css`**（浅色主题变量，小夕电商蓝 primary + 暖色 accent）

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

:root {
  --background: 220 20% 97%;
  --foreground: 222 20% 18%;
  --card: 0 0% 100%;
  --card-foreground: 222 20% 18%;
  --primary: 226 100% 65%;            /* 电商蓝 ≈ #4f7cff */
  --primary-foreground: 0 0% 100%;
  --secondary: 220 14% 94%;
  --secondary-foreground: 222 20% 24%;
  --muted: 220 14% 94%;
  --muted-foreground: 220 9% 46%;
  --accent: 28 96% 55%;               /* 暖色（议价/优惠强调）*/
  --accent-foreground: 0 0% 100%;
  --destructive: 0 72% 51%;
  --destructive-foreground: 0 0% 100%;
  --border: 220 13% 88%;
  --input: 220 13% 85%;
  --ring: 226 100% 65%;
  --radius: 0.75rem;
}

* { border-color: hsl(var(--border)); }
html, body, #root { height: 100%; }
body { margin: 0; background: hsl(var(--background)); color: hsl(var(--foreground)); font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
```

- [ ] **Step 7: 建 `webui/src/main.tsx`、`webui/src/App.tsx`（占位）、`webui/src/vite-env.d.ts`、`webui/src/test/setup.ts`、`webui/.gitignore`**

`webui/src/main.tsx`:
```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./globals.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```
`webui/src/App.tsx`（占位，后续任务替换）:
```tsx
export default function App() {
  return <div className="p-6 text-foreground">小夕 WebUI 脚手架就绪</div>;
}
```
`webui/src/vite-env.d.ts`:
```ts
/// <reference types="vite/client" />
```
`webui/src/test/setup.ts`:
```ts
import "@testing-library/jest-dom";
```
`webui/.gitignore`:
```
node_modules
*.tsbuildinfo
```

- [ ] **Step 8: 安装依赖并构建，验证产物**

Run: `cd /Users/shine/ecom-service-agent/webui && npm install && npm run build`
Expected: 无报错；生成 `/Users/shine/ecom-service-agent/web/dist/index.html` 与 `web/dist/assets/*.js`。
校验：`ls /Users/shine/ecom-service-agent/web/dist/index.html` 存在。

- [ ] **Step 9: 根仓库 .gitignore 忽略 node_modules（不忽略 web/dist）**

确认 `/Users/shine/ecom-service-agent/.gitignore` 含 `webui/node_modules`（若无则追加一行）。**不要**忽略 `web/dist`。

- [ ] **Step 10: Commit**

```bash
cd /Users/shine/ecom-service-agent
git add webui/package.json webui/index.html webui/vite.config.ts webui/tsconfig.json webui/tsconfig.node.json webui/postcss.config.js webui/tailwind.config.js webui/src/globals.css webui/src/main.tsx webui/src/App.tsx webui/src/vite-env.d.ts webui/src/test/setup.ts webui/.gitignore .gitignore
git commit -m "feat(webui): Vite+React+TS+Tailwind 脚手架，产物输出 web/dist"
```
（注：本任务先不提交 `web/dist` 产物，留到 Task 8 统一构建后提交。）

---

### Task 2: 后端托管 SPA + /legacy 旧页

**Files:** Modify `app/api/app.py`；Test `tests/test_webui_serving.py`

- [ ] **Step 1: 写失败测试** `tests/test_webui_serving.py`

```python
from fastapi.testclient import TestClient
from app.api.app import create_app


def test_root_serves_spa_or_legacy():
    app = create_app()
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert "<div id=\"root\">" in r.text or "小夕" in r.text  # dist 存在则 SPA，否则回退旧页


def test_legacy_serves_old_page():
    app = create_app()
    c = TestClient(app)
    r = c.get("/legacy")
    assert r.status_code == 200
    assert "switchTab" in r.text  # 旧 chat.html 的标志


def test_health_still_ok():
    app = create_app()
    c = TestClient(app)
    assert c.get("/api/health").json() == {"status": "ok"}
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/shine/ecom-service-agent && source .venv/bin/activate && pytest tests/test_webui_serving.py -v`
Expected: `test_legacy_serves_old_page` FAIL（/legacy 未定义，404）。

- [ ] **Step 3: 改 `app/api/app.py`**

顶部 import 增加：
```python
from fastapi.staticfiles import StaticFiles
```
在 `_WEB_DIR = ...` 之后加：
```python
_DIST_DIR = _WEB_DIR / "dist"
```
在 `create_app` 内、`return app` 之前，替换现有 `/` 与 `/dashboard` 路由为如下（新增 `/legacy`，`/` 优先 SPA、回退旧页；`/dashboard` 保留为旧页别名以兼容）：
```python
    # 新版 SPA（web/dist）；未构建时回退旧页，保证始终可用
    if (_DIST_DIR / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=str(_DIST_DIR / "assets")), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        spa = _DIST_DIR / "index.html"
        if spa.exists():
            return HTMLResponse(spa.read_text(encoding="utf-8"))
        return HTMLResponse((_WEB_DIR / "chat.html").read_text(encoding="utf-8"))

    @app.get("/legacy", response_class=HTMLResponse)
    def legacy():
        return HTMLResponse((_WEB_DIR / "chat.html").read_text(encoding="utf-8"))

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        # 兼容旧链接：看板已并入新 SPA / 旧页
        spa = _DIST_DIR / "index.html"
        if spa.exists():
            return HTMLResponse(spa.read_text(encoding="utf-8"))
        return HTMLResponse((_WEB_DIR / "chat.html").read_text(encoding="utf-8"))
```
（删除原来的 `@app.get("/")` index 和 `@app.get("/dashboard")` dashboard 两个旧定义，避免重复路由。）

- [ ] **Step 4: 运行确认通过**

Run: `cd /Users/shine/ecom-service-agent && source .venv/bin/activate && pytest tests/test_webui_serving.py -v`
Expected: 3 passed（此时 `web/dist` 已由 Task 1 构建存在 → `/` 返回 SPA；`/legacy` 返回旧页）。

- [ ] **Step 5: Commit**

```bash
git add app/api/app.py tests/test_webui_serving.py
git commit -m "feat(webui): FastAPI 托管 web/dist SPA + /legacy 旧页回退"
```

---

### Task 3: lib（utils/api/sse）+ useChatStream（TDD）

**Files:** Create `webui/src/lib/utils.ts`、`webui/src/lib/api.ts`、`webui/src/lib/sse.ts`、`webui/src/hooks/useChatStream.ts`；Test `webui/src/tests/sse.test.ts`、`webui/src/tests/useChatStream.test.ts`

- [ ] **Step 1: 写失败测试** `webui/src/tests/sse.test.ts`

```ts
import { describe, it, expect } from "vitest";
import { splitSSEFrames } from "@/lib/sse";

describe("splitSSEFrames", () => {
  it("拆出完整帧，保留半帧余量", () => {
    const { events, rest } = splitSSEFrames(
      'data: {"type":"thought","content":"a"}\n\ndata: {"type":"reply","content":"hi"}\n\ndata: {"type":"do'
    );
    expect(events).toEqual([
      { type: "thought", content: "a" },
      { type: "reply", content: "hi" },
    ]);
    expect(rest).toBe('data: {"type":"do');
  });

  it("无完整帧时全部留作余量", () => {
    const { events, rest } = splitSSEFrames('data: {"type":"x"');
    expect(events).toEqual([]);
    expect(rest).toBe('data: {"type":"x"');
  });
});
```

`webui/src/tests/useChatStream.test.ts`：
```ts
import { describe, it, expect, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useChatStream } from "@/hooks/useChatStream";

function mockSSEResponse(frames: string[]) {
  const enc = new TextEncoder();
  let i = 0;
  const body = {
    getReader() {
      return {
        read() {
          if (i < frames.length) return Promise.resolve({ value: enc.encode(frames[i++]), done: false });
          return Promise.resolve({ value: undefined, done: true });
        },
      };
    },
  };
  return { ok: true, body } as unknown as Response;
}

describe("useChatStream", () => {
  it("把 SSE 事件回调出来", async () => {
    const events: any[] = [];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockSSEResponse([
      'data: {"type":"thought","content":"想一下"}\n\n',
      'data: {"type":"reply","content":"你好"}\n\ndata: {"type":"metadata","intent":"greeting","confidence":0.9,"requires_human":false}\n\n',
      'data: {"type":"done"}\n\n',
    ])));
    const { result } = renderHook(() => useChatStream({ sessionId: "s1", onEvent: (e) => events.push(e) }));
    await act(async () => { await result.current.send("在吗"); });
    await waitFor(() => expect(events.at(-1)?.type).toBe("done"));
    expect(events.map((e) => e.type)).toContain("reply");
    expect(events.find((e) => e.type === "metadata").intent).toBe("greeting");
    vi.unstubAllGlobals();
  });
});
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/shine/ecom-service-agent/webui && npm run test`
Expected: FAIL（`@/lib/sse` 等模块不存在）。

- [ ] **Step 3: 实现 lib 与 hook**

`webui/src/lib/utils.ts`:
```ts
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
```
`webui/src/lib/sse.ts`:
```ts
export type SSEEvent = Record<string, any> & { type: string };

/** 把累积字符串按 \n\n 拆成事件；返回解析出的事件与未完成的余量。 */
export function splitSSEFrames(buffer: string): { events: SSEEvent[]; rest: string } {
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";
  const events: SSEEvent[] = [];
  for (const frame of parts) {
    const line = frame.trim();
    if (!line.startsWith("data:")) continue;
    try {
      events.push(JSON.parse(line.slice(line.indexOf(":") + 1).trim()));
    } catch {
      /* 忽略坏帧 */
    }
  }
  return { events, rest };
}
```
`webui/src/lib/api.ts`:
```ts
export function getSessionId(): string {
  let s = localStorage.getItem("xiaoxi_sid");
  if (!s) { s = "web-" + Math.random().toString(36).slice(2, 10); localStorage.setItem("xiaoxi_sid", s); }
  return s;
}
export function adminFetch(url: string, opts: RequestInit = {}) {
  const token = localStorage.getItem("admin_token") || "";
  opts.headers = { ...(opts.headers || {}), ...(token ? { "X-Admin-Token": token } : {}) };
  return fetch(url, opts);
}
export async function getJSON<T>(url: string): Promise<T> {
  const r = await adminFetch(url);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
```
`webui/src/hooks/useChatStream.ts`:
```ts
import { useCallback, useState } from "react";
import { splitSSEFrames, type SSEEvent } from "@/lib/sse";

export function useChatStream(opts: { sessionId: string; onEvent: (e: SSEEvent) => void }) {
  const [streaming, setStreaming] = useState(false);

  const send = useCallback(async (message: string) => {
    setStreaming(true);
    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: opts.sessionId, message }),
      });
      if (!resp.ok || !resp.body) throw new Error("服务返回 " + resp.status);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const { events, rest } = splitSSEFrames(buf);
        buf = rest;
        for (const e of events) opts.onEvent(e);
      }
    } catch (err: any) {
      opts.onEvent({ type: "error", message: err?.message || "连接失败" });
    } finally {
      setStreaming(false);
    }
  }, [opts]);

  return { send, streaming };
}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd /Users/shine/ecom-service-agent/webui && npm run test`
Expected: sse.test 与 useChatStream.test 全部 PASS。

- [ ] **Step 5: Commit**

```bash
cd /Users/shine/ecom-service-agent
git add webui/src/lib webui/src/hooks/useChatStream.ts webui/src/tests/sse.test.ts webui/src/tests/useChatStream.test.ts
git commit -m "feat(webui): api/sse/useChatStream + 单测"
```

---

### Task 4: shadcn/ui 原语

**Files（均 Create）:** `webui/src/components/ui/button.tsx`、`card.tsx`、`badge.tsx`、`dialog.tsx`、`separator.tsx`、`scroll-area.tsx`

- [ ] **Step 1: 建 `button.tsx`**

```tsx
import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground hover:opacity-90",
        secondary: "bg-secondary text-secondary-foreground hover:bg-muted",
        ghost: "hover:bg-secondary",
        destructive: "bg-destructive text-destructive-foreground hover:opacity-90",
        outline: "border bg-card hover:bg-secondary",
      },
      size: { default: "h-10 px-4 py-2", sm: "h-8 px-3 text-xs", icon: "h-9 w-9" },
    },
    defaultVariants: { variant: "default", size: "default" },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}
export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return <Comp ref={ref} className={cn(buttonVariants({ variant, size, className }))} {...props} />;
  }
);
Button.displayName = "Button";
export { buttonVariants };
```

- [ ] **Step 2: 建 `card.tsx`**

```tsx
import * as React from "react";
import { cn } from "@/lib/utils";

export const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("rounded-lg border bg-card text-card-foreground shadow-sm", className)} {...props} />
  )
);
Card.displayName = "Card";
export function CardHeader({ className, ...p }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4 pb-2", className)} {...p} />;
}
export function CardContent({ className, ...p }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-4 pt-2", className)} {...p} />;
}
```

- [ ] **Step 3: 建 `badge.tsx`**

```tsx
import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium",
  {
    variants: {
      variant: {
        default: "border-transparent bg-secondary text-secondary-foreground",
        primary: "border-transparent bg-primary/10 text-primary",
        accent: "border-transparent bg-accent/15 text-accent",
        destructive: "border-transparent bg-destructive/10 text-destructive",
        outline: "text-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  }
);
export function Badge({ className, variant, ...props }: React.HTMLAttributes<HTMLDivElement> & VariantProps<typeof badgeVariants>) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}
```

- [ ] **Step 4: 建 `dialog.tsx`**（Radix 封装）

```tsx
import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;

export const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>
>(({ className, children, ...props }, ref) => (
  <DialogPrimitive.Portal>
    <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/40" />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        "fixed left-1/2 top-1/2 z-50 w-[92vw] max-w-2xl -translate-x-1/2 -translate-y-1/2 rounded-lg border bg-card p-5 shadow-lg",
        className
      )}
      {...props}
    >
      {children}
      <DialogPrimitive.Close className="absolute right-4 top-4 opacity-60 hover:opacity-100">
        <X className="h-4 w-4" />
      </DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPrimitive.Portal>
));
DialogContent.displayName = "DialogContent";
export function DialogTitle(props: React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>) {
  return <DialogPrimitive.Title className="text-base font-semibold" {...props} />;
}
```

- [ ] **Step 5: 建 `separator.tsx` 与 `scroll-area.tsx`**

`separator.tsx`:
```tsx
import * as SeparatorPrimitive from "@radix-ui/react-separator";
import { cn } from "@/lib/utils";
export function Separator({ className, ...props }: React.ComponentPropsWithoutRef<typeof SeparatorPrimitive.Root>) {
  return <SeparatorPrimitive.Root className={cn("shrink-0 bg-border h-px w-full", className)} {...props} />;
}
```
`scroll-area.tsx`（简化：原生滚动容器即可）:
```tsx
import * as React from "react";
import { cn } from "@/lib/utils";
export function ScrollArea({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("overflow-y-auto", className)} {...props} />;
}
```

- [ ] **Step 6: 类型检查通过**

Run: `cd /Users/shine/ecom-service-agent/webui && npx tsc -b`
Expected: 无类型错误（无输出即通过）。

- [ ] **Step 7: Commit**

```bash
cd /Users/shine/ecom-service-agent
git add webui/src/components/ui
git commit -m "feat(webui): shadcn/ui 原语(button/card/badge/dialog/separator/scroll-area)"
```

---

### Task 5: App 外壳 + Sidebar + 导航（浅色小夕品牌）

**Files:** Create `webui/src/components/AppShell.tsx`、`webui/src/components/Sidebar.tsx`；Modify `webui/src/App.tsx`

- [ ] **Step 1: 建 `Sidebar.tsx`**

```tsx
import { MessageSquare, LayoutDashboard, RotateCcw, ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export type View = "chat" | "dash";

export function Sidebar({ view, onView, onReset }: { view: View; onView: (v: View) => void; onReset: () => void }) {
  const item = (v: View, icon: React.ReactNode, label: string) => (
    <button
      onClick={() => onView(v)}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
        view === v ? "bg-primary text-primary-foreground" : "hover:bg-secondary text-foreground"
      )}
    >
      {icon} {label}
    </button>
  );
  return (
    <aside className="flex w-60 shrink-0 flex-col gap-2 border-r bg-card p-3">
      <div className="flex items-center gap-2 px-2 py-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-primary-foreground font-bold">夕</div>
        <div>
          <div className="text-sm font-semibold leading-tight">并夕夕 · 小夕</div>
          <div className="text-xs text-muted-foreground">智能客服</div>
        </div>
      </div>
      <nav className="flex flex-col gap-1">
        {item("chat", <MessageSquare className="h-4 w-4" />, "聊天")}
        {item("dash", <LayoutDashboard className="h-4 w-4" />, "看板")}
      </nav>
      <div className="mt-auto flex flex-col gap-1">
        <Button variant="ghost" size="sm" className="justify-start" onClick={onReset}>
          <RotateCcw className="h-4 w-4" /> 重置对话
        </Button>
        <a href="/legacy" className="flex items-center gap-2 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-secondary">
          <ExternalLink className="h-4 w-4" /> 更多（坐席/评估）
        </a>
      </div>
    </aside>
  );
}
```

- [ ] **Step 2: 建 `AppShell.tsx`**

```tsx
import { Sidebar, type View } from "@/components/Sidebar";

export function AppShell({ view, onView, onReset, children }: {
  view: View; onView: (v: View) => void; onReset: () => void; children: React.ReactNode;
}) {
  return (
    <div className="flex h-full">
      <Sidebar view={view} onView={onView} onReset={onReset} />
      <main className="flex-1 min-w-0 overflow-hidden">{children}</main>
    </div>
  );
}
```

- [ ] **Step 3: 改 `App.tsx`** 组织视图与会话/重置

```tsx
import { useState } from "react";
import { AppShell } from "@/components/AppShell";
import type { View } from "@/components/Sidebar";
import { ChatView } from "@/components/ChatView";
import { DashboardView } from "@/components/DashboardView";
import { adminFetch, getSessionId } from "@/lib/api";

export default function App() {
  const [view, setView] = useState<View>("chat");
  const [resetKey, setResetKey] = useState(0);
  const sessionId = getSessionId();

  async function onReset() {
    await adminFetch("/api/session/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    });
    setResetKey((k) => k + 1);
    setView("chat");
  }

  return (
    <AppShell view={view} onView={setView} onReset={onReset}>
      {view === "chat" ? <ChatView key={resetKey} sessionId={sessionId} /> : <DashboardView sessionId={sessionId} />}
    </AppShell>
  );
}
```
（此时 `ChatView`/`DashboardView` 尚未建，Task 6/7 建。可先建空占位以便类型通过，或直接进入 Task 6/7 再回来验证；为让本任务可独立验证，先建最小占位见 Step 4。）

- [ ] **Step 4: 临时占位**（让脚手架能编译；Task 6/7 会替换为真实实现）

`webui/src/components/ChatView.tsx`:
```tsx
export function ChatView({ sessionId }: { sessionId: string }) {
  return <div className="p-6">聊天页占位 · {sessionId}</div>;
}
```
`webui/src/components/DashboardView.tsx`:
```tsx
export function DashboardView({ sessionId }: { sessionId: string }) {
  return <div className="p-6">看板页占位 · {sessionId}</div>;
}
```

- [ ] **Step 5: 类型检查 + 构建通过**

Run: `cd /Users/shine/ecom-service-agent/webui && npx tsc -b && npm run build`
Expected: 无错误，`web/dist/index.html` 更新。

- [ ] **Step 6: Commit**

```bash
cd /Users/shine/ecom-service-agent
git add webui/src/components/AppShell.tsx webui/src/components/Sidebar.tsx webui/src/App.tsx webui/src/components/ChatView.tsx webui/src/components/DashboardView.tsx
git commit -m "feat(webui): App 外壳 + Sidebar 导航(浅色小夕品牌)"
```

---

### Task 6: 聊天页（消息/活动流/元信息/输入）+ 测试

**Files:** Create `webui/src/components/MessageBubble.tsx`、`AgentActivity.tsx`、`MetadataChips.tsx`、`Composer.tsx`；Replace `webui/src/components/ChatView.tsx`；Test `webui/src/tests/chat-components.test.tsx`

数据模型（在 ChatView 内维护）：一轮 = `{ id, userText, activity: SSEEvent[], reply?: string, meta?: {...}, handoff?: {...} }`。`useChatStream` 的 `onEvent` 把事件按类型塞进当前轮。

- [ ] **Step 1: 写失败测试** `webui/src/tests/chat-components.test.tsx`

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MetadataChips } from "@/components/MessageBubble"; // 见 Step 3：MetadataChips 从 MessageBubble 导出或单独文件
import { AgentActivity } from "@/components/AgentActivity";

describe("MetadataChips", () => {
  it("渲染意图/置信度/转人工", () => {
    render(<MetadataChips meta={{ intent: "order_query", confidence: 0.83, requires_human: false }} />);
    expect(screen.getByText(/order_query/)).toBeInTheDocument();
    expect(screen.getByText(/83%/)).toBeInTheDocument();
    expect(screen.getByText(/否/)).toBeInTheDocument();
  });
});

describe("AgentActivity", () => {
  it("渲染思考与工具事件", () => {
    render(<AgentActivity events={[
      { type: "thought", content: "先查订单" },
      { type: "tool_call", name: "query_order", args: { order_id: "X" } },
      { type: "tool_result", content: "{\"ok\":true}" },
    ]} defaultOpen />);
    expect(screen.getByText(/先查订单/)).toBeInTheDocument();
    expect(screen.getByText(/query_order/)).toBeInTheDocument();
  });
});
```
（注：`MetadataChips` 请放在单独文件 `MetadataChips.tsx` 并从那里导入；上面 import 路径改为 `@/components/MetadataChips`。）

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/shine/ecom-service-agent/webui && npm run test`
Expected: FAIL（组件不存在）。

- [ ] **Step 3: 实现组件**

`webui/src/components/MetadataChips.tsx`:
```tsx
import { Badge } from "@/components/ui/badge";

const INTENT_CN: Record<string, string> = {
  order_query: "订单查询", return_request: "退换货", product_consult: "商品咨询",
  complaint: "投诉", after_sale: "售后", promotion: "优惠活动", account: "账户",
  greeting: "打招呼", other: "其他",
};
export type Meta = { intent: string; confidence: number; requires_human: boolean; follow_up_question?: string | null };

export function MetadataChips({ meta }: { meta: Meta }) {
  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      <Badge variant="primary">意图: {INTENT_CN[meta.intent] || meta.intent}</Badge>
      <Badge variant="default">置信度: {(meta.confidence * 100).toFixed(0)}%</Badge>
      <Badge variant={meta.requires_human ? "destructive" : "default"}>转人工: {meta.requires_human ? "是" : "否"}</Badge>
    </div>
  );
}
```
`webui/src/components/AgentActivity.tsx`:
```tsx
import { useState } from "react";
import { Brain, Wrench, ClipboardList, ShieldAlert, ChevronDown, ChevronRight } from "lucide-react";
import type { SSEEvent } from "@/lib/sse";
import { cn } from "@/lib/utils";

export function AgentActivity({ events, defaultOpen = false }: { events: SSEEvent[]; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  if (!events.length) return null;
  return (
    <div className="my-1.5 rounded-lg border bg-secondary/50 text-xs">
      <button className="flex w-full items-center gap-1 px-3 py-2 text-muted-foreground" onClick={() => setOpen((o) => !o)}>
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        🧠 Agent 思考过程（{events.length}）
      </button>
      {open && (
        <div className="flex flex-col gap-1 px-3 pb-2">
          {events.map((e, i) => (
            <div key={i} className="flex items-start gap-1.5">
              {e.type === "thought" && <><Brain className="mt-0.5 h-3.5 w-3.5 text-primary" /><span>{e.content}</span></>}
              {e.type === "tool_call" && <><Wrench className="mt-0.5 h-3.5 w-3.5 text-accent" /><span className="font-mono">{e.name}({JSON.stringify(e.args)})</span></>}
              {e.type === "tool_result" && <><ClipboardList className="mt-0.5 h-3.5 w-3.5 text-muted-foreground" /><span className="font-mono text-muted-foreground">{String(e.content).slice(0, 200)}</span></>}
              {e.type === "guard" && <><ShieldAlert className="mt-0.5 h-3.5 w-3.5 text-destructive" /><span>护栏[{e.stage}/{e.action}] {e.reason}</span></>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```
`webui/src/components/MessageBubble.tsx`:
```tsx
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

export function MessageBubble({ role, children }: { role: "user" | "assistant"; children: string }) {
  const isUser = role === "user";
  return (
    <div className={cn("flex", isUser ? "justify-end" : "justify-start")}>
      <div className={cn(
        "max-w-[78%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
        isUser ? "bg-primary text-primary-foreground" : "border bg-card"
      )}>
        {isUser ? (
          <span className="whitespace-pre-wrap">{children}</span>
        ) : (
          <div className="prose prose-sm max-w-none prose-p:my-1">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  );
}
```
`webui/src/components/Composer.tsx`:
```tsx
import { useState } from "react";
import { Send } from "lucide-react";
import { Button } from "@/components/ui/button";

export function Composer({ disabled, onSend }: { disabled: boolean; onSend: (t: string) => void }) {
  const [text, setText] = useState("");
  function submit() {
    const t = text.trim();
    if (!t || disabled) return;
    onSend(t); setText("");
  }
  return (
    <div className="flex items-end gap-2 border-t bg-card p-3">
      <textarea
        className="flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        rows={1} placeholder="试试：我的订单还没发货，怎么回事？"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
      />
      <Button disabled={disabled} onClick={submit}><Send className="h-4 w-4" /> 发送</Button>
    </div>
  );
}
```

- [ ] **Step 4: 实现 `ChatView.tsx`（替换占位）**

```tsx
import { useRef, useState } from "react";
import { useChatStream } from "@/hooks/useChatStream";
import type { SSEEvent } from "@/lib/sse";
import { MessageBubble } from "@/components/MessageBubble";
import { AgentActivity } from "@/components/AgentActivity";
import { MetadataChips, type Meta } from "@/components/MetadataChips";
import { Composer } from "@/components/Composer";
import { ScrollArea } from "@/components/ui/scroll-area";

type Turn = { id: number; userText: string; activity: SSEEvent[]; reply?: string; meta?: Meta; handoff?: string[] };

export function ChatView({ sessionId }: { sessionId: string }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const idRef = useRef(0);
  const cur = useRef<number>(-1);

  const patch = (fn: (t: Turn) => Turn) =>
    setTurns((ts) => ts.map((t) => (t.id === cur.current ? fn(t) : t)));

  const { send, streaming } = useChatStream({
    sessionId,
    onEvent: (e) => {
      if (["thought", "tool_call", "tool_result", "guard"].includes(e.type)) patch((t) => ({ ...t, activity: [...t.activity, e] }));
      else if (e.type === "reply") patch((t) => ({ ...t, reply: e.content }));
      else if (e.type === "metadata") patch((t) => ({ ...t, meta: e as Meta }));
      else if (e.type === "handoff") patch((t) => ({ ...t, handoff: e.reasons || [] }));
      else if (e.type === "error") patch((t) => ({ ...t, reply: "⚠️ 出错了：" + e.message }));
    },
  });

  function onSend(text: string) {
    const id = ++idRef.current;
    cur.current = id;
    setTurns((ts) => [...ts, { id, userText: text, activity: [] }]);
    send(text);
  }

  return (
    <div className="flex h-full flex-col">
      <ScrollArea className="flex-1">
        <div className="mx-auto flex max-w-3xl flex-col gap-4 p-6">
          {turns.length === 0 && <div className="mt-20 text-center text-muted-foreground">你好，我是小夕 😊 有什么可以帮你？</div>}
          {turns.map((t) => (
            <div key={t.id} className="flex flex-col gap-1">
              <MessageBubble role="user">{t.userText}</MessageBubble>
              <AgentActivity events={t.activity} defaultOpen={!t.reply} />
              {t.handoff && <div className="rounded-md bg-accent/15 px-3 py-2 text-sm text-accent">🎧 已转人工，原因：{t.handoff.join("、")}</div>}
              {t.reply && <MessageBubble role="assistant">{t.reply}</MessageBubble>}
              {t.meta && <MetadataChips meta={t.meta} />}
            </div>
          ))}
        </div>
      </ScrollArea>
      <div className="mx-auto w-full max-w-3xl">
        <Composer disabled={streaming} onSend={onSend} />
      </div>
    </div>
  );
}
```

- [ ] **Step 5: 运行测试 + 构建通过**

Run: `cd /Users/shine/ecom-service-agent/webui && npm run test && npx tsc -b && npm run build`
Expected: 组件测试 PASS，类型/构建无错误。

- [ ] **Step 6: Commit**

```bash
cd /Users/shine/ecom-service-agent
git add webui/src/components/MessageBubble.tsx webui/src/components/AgentActivity.tsx webui/src/components/MetadataChips.tsx webui/src/components/Composer.tsx webui/src/components/ChatView.tsx webui/src/tests/chat-components.test.tsx
git commit -m "feat(webui): 聊天页(气泡/Markdown/活动流/元信息/输入)+组件测试"
```

---

### Task 7: 看板页（指标卡/意图/请求表/调用链）+ 测试

**Files:** Create `webui/src/components/MetricCards.tsx`、`TracesTable.tsx`；Replace `webui/src/components/DashboardView.tsx`；Test `webui/src/tests/dashboard.test.tsx`

- [ ] **Step 1: 写失败测试** `webui/src/tests/dashboard.test.tsx`

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MetricCards } from "@/components/MetricCards";

const M = {
  total_traces: 12, error_rate: 0.0, latency_p50_ms: 800, latency_p95_ms: 1500,
  tool_success_rate: 1.0, tool_calls: 5, guard_blocks: 1, block_rate: 0.08,
  guard_sanitizes: 2, handoffs: 0, escalation_rate: 0.0,
  total_prompt_tokens: 100, total_completion_tokens: 50, est_cost_usd: 0.01,
  intent_distribution: { order_query: 3, greeting: 2 },
};

describe("MetricCards", () => {
  it("渲染核心指标", () => {
    render(<MetricCards m={M as any} />);
    expect(screen.getByText("总请求数")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText(/P95/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/shine/ecom-service-agent/webui && npm run test`
Expected: FAIL（`MetricCards` 不存在）。

- [ ] **Step 3: 实现组件**

`webui/src/components/MetricCards.tsx`:
```tsx
import { Card } from "@/components/ui/card";

export type Metrics = {
  total_traces: number; error_rate: number; latency_p50_ms: number; latency_p95_ms: number;
  tool_success_rate: number; tool_calls: number; guard_blocks: number; block_rate: number;
  guard_sanitizes: number; handoffs: number; escalation_rate: number;
  total_prompt_tokens: number; total_completion_tokens: number; est_cost_usd: number;
  intent_distribution: Record<string, number>;
};
const pct = (x: number) => (x * 100).toFixed(1) + "%";
const ms = (x: number) => x.toFixed(0) + " ms";

export function MetricCards({ m }: { m: Metrics }) {
  const items: [string, string | number][] = [
    ["总请求数", m.total_traces], ["错误率", pct(m.error_rate)],
    ["延迟 P50", ms(m.latency_p50_ms)], ["延迟 P95", ms(m.latency_p95_ms)],
    ["工具成功率", pct(m.tool_success_rate)], ["工具调用数", m.tool_calls],
    ["护栏拦截", m.guard_blocks], ["拦截率", pct(m.block_rate)],
    ["脱敏次数", m.guard_sanitizes], ["转人工数", m.handoffs],
    ["转人工率", pct(m.escalation_rate)], ["估算成本", "$" + m.est_cost_usd],
    ["Prompt tokens", m.total_prompt_tokens], ["Completion tokens", m.total_completion_tokens],
  ];
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
      {items.map(([label, value]) => (
        <Card key={label} className="p-4">
          <div className="text-xs text-muted-foreground">{label}</div>
          <div className="mt-1 text-2xl font-semibold">{value}</div>
        </Card>
      ))}
    </div>
  );
}
```
`webui/src/components/TracesTable.tsx`:
```tsx
import { useState } from "react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { adminFetch } from "@/lib/api";

export type Trace = { trace_id: string; started_at: number; session_id?: string; intent?: string; status: string; latency_ms: number; prompt_tokens?: number; completion_tokens?: number };

export function TracesTable({ traces }: { traces: Trace[] }) {
  const [detail, setDetail] = useState<any | null>(null);
  async function open(id: string) {
    const t = await (await adminFetch("/api/traces/" + id)).json();
    setDetail(t);
  }
  return (
    <>
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead className="bg-secondary/60 text-muted-foreground">
            <tr>{["时间(相对)", "会话", "意图", "状态", "延迟(ms)", "tokens"].map((h) => <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>)}</tr>
          </thead>
          <tbody>
            {traces.length === 0 && <tr><td colSpan={6} className="px-3 py-6 text-center text-muted-foreground">暂无记录</td></tr>}
            {traces.map((t) => (
              <tr key={t.trace_id} className="cursor-pointer border-t hover:bg-secondary/40" onClick={() => open(t.trace_id)}>
                <td className="px-3 py-2">{t.started_at.toFixed(0)}</td>
                <td className="px-3 py-2" title={t.session_id}>{(t.session_id || "-").slice(0, 12)}</td>
                <td className="px-3 py-2">{t.intent || "-"}</td>
                <td className={"px-3 py-2 " + (t.status === "ok" ? "text-green-600" : "text-destructive")}>{t.status}</td>
                <td className="px-3 py-2">{t.latency_ms.toFixed(0)}</td>
                <td className="px-3 py-2">{(t.prompt_tokens || 0)}+{(t.completion_tokens || 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Dialog open={!!detail} onOpenChange={(o) => !o && setDetail(null)}>
        <DialogContent>
          <DialogTitle>调用链 {detail?.trace_id}</DialogTitle>
          <pre className="mt-3 max-h-[60vh] overflow-auto rounded-md bg-secondary p-3 text-xs">
{detail && `用户: ${detail.user_input}\n意图: ${detail.intent} 状态: ${detail.status}\n\n` +
  (detail?.spans || []).map((s: any) => `[${s.kind}] ${s.name}  ${s.latency_ms.toFixed(0)}ms` +
    (s.success == null ? "" : s.success ? " ✅" : " ❌") +
    (s.prompt_tokens ? `  tok:${s.prompt_tokens}+${s.completion_tokens}` : "")).join("\n")}
          </pre>
        </DialogContent>
      </Dialog>
    </>
  );
}
```

- [ ] **Step 4: 实现 `DashboardView.tsx`（替换占位）**

```tsx
import { useEffect, useState } from "react";
import { getJSON } from "@/lib/api";
import { MetricCards, type Metrics } from "@/components/MetricCards";
import { TracesTable, type Trace } from "@/components/TracesTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export function DashboardView({ sessionId }: { sessionId: string }) {
  const [m, setM] = useState<Metrics | null>(null);
  const [traces, setTraces] = useState<Trace[]>([]);
  const [onlyMine, setOnlyMine] = useState(true);

  async function load() {
    setM(await getJSON<Metrics>("/api/metrics"));
    const url = "/api/traces?limit=50" + (onlyMine ? "&session_id=" + encodeURIComponent(sessionId) : "");
    setTraces(await getJSON<Trace[]>(url));
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [onlyMine]);

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-5xl flex-col gap-5">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">可观测看板</h2>
          <Button variant="secondary" size="sm" onClick={load}>刷新</Button>
        </div>
        {m ? <MetricCards m={m} /> : <div className="text-muted-foreground">加载中…</div>}
        {m && (
          <div>
            <h3 className="mb-2 text-sm font-medium">意图分布</h3>
            <div className="flex flex-wrap gap-2">
              {Object.entries(m.intent_distribution).map(([k, v]) => <Badge key={k} variant="outline">{k}: {v}</Badge>)}
            </div>
          </div>
        )}
        <div>
          <div className="mb-2 flex items-center gap-3">
            <h3 className="text-sm font-medium">最近请求（点行看调用链）</h3>
            <label className="flex items-center gap-1 text-xs text-muted-foreground">
              <input type="checkbox" checked={onlyMine} onChange={(e) => setOnlyMine(e.target.checked)} /> 只看本会话
            </label>
          </div>
          <TracesTable traces={traces} />
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: 运行测试 + 构建通过**

Run: `cd /Users/shine/ecom-service-agent/webui && npm run test && npx tsc -b && npm run build`
Expected: 全部 PASS，构建无错误。

- [ ] **Step 6: Commit**

```bash
cd /Users/shine/ecom-service-agent
git add webui/src/components/MetricCards.tsx webui/src/components/TracesTable.tsx webui/src/components/DashboardView.tsx webui/src/tests/dashboard.test.tsx
git commit -m "feat(webui): 看板页(指标卡/意图/请求表/调用链)+测试"
```

---

### Task 8: 构建产物入库 + README + 端到端验证

**Files:** 构建 `web/dist/*`（提交）；Modify `README.md`

- [ ] **Step 1: 全量前端测试 + 生产构建**

Run: `cd /Users/shine/ecom-service-agent/webui && npm run test && npm run build`
Expected: 测试全绿；`web/dist/index.html` + `web/dist/assets/*` 生成。

- [ ] **Step 2: 后端测试无回归**

Run: `cd /Users/shine/ecom-service-agent && source .venv/bin/activate && pytest tests/test_webui_serving.py -v`
Expected: 3 passed。

- [ ] **Step 3: 端到端冒烟（人工/脚本）**

启动：`cd /Users/shine/ecom-service-agent && source .venv/bin/activate && python run_api.py`（另一窗口）。
- `curl -s http://127.0.0.1:8010/ | grep -o 'id="root"'` → 应输出 `id="root"`（SPA 生效）。
- `curl -s http://127.0.0.1:8010/legacy | grep -o switchTab` → 应输出 `switchTab`（旧页可用）。
- `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8010/api/health` → `200`。
- 浏览器开 `http://127.0.0.1:8010/`：发一句「Nike Air Max 270 能便宜点吗」→ 看到活动流(思考/工具/negotiate_price)、Markdown 回复、意图/置信度徽章；切「看板」→ 指标卡 + 请求表 + 点行看调用链；点「更多」→ 打开 `/legacy`。

- [ ] **Step 4: 更新 README「前端」章节**

在 `README.md` 增补：
```markdown
## 前端（Web UI）

新版界面由 `webui/`（React + Vite + Tailwind + shadcn/ui）构建，产物在 `web/dist/`（已入库）。
- 直接运行：`python run_api.py` 后打开 http://127.0.0.1:8010/ 即为新界面（无需 npm）。
- 修改前端：`cd webui && npm install && npm run dev`（:5173，代理 /api → 8010）；改完 `npm run build` 重新生成并提交 `web/dist/`。
- 旧页（坐席/评估）仍在 http://127.0.0.1:8010/legacy 。
```

- [ ] **Step 5: 提交构建产物与 README**

```bash
cd /Users/shine/ecom-service-agent
git add web/dist README.md
git commit -m "feat(webui): 提交构建产物 web/dist + README 前端说明"
```

---

## Self-Review 记录

- **Spec 覆盖**：技术栈→Task1；产物到 web/dist→Task1/8；后端托管+/legacy→Task2；SSE/契约复用→Task3；shadcn 原语→Task4；外壳/侧边栏/浅色品牌→Task5；聊天页(气泡/Markdown/活动流/元信息/转人工/输入)→Task6；看板(指标/意图/请求表/调用链/只看本会话)→Task7；提交 web/dist + README + 端到端→Task8；测试三处(sse/useChatStream、聊天组件、看板)→Task3/6/7。全部有对应任务。
- **占位符**：无 TBD；配置/lib/hook/原语给了完整代码，组件给了完整 TSX。
- **类型/命名一致性**：`SSEEvent`、`Meta`、`Metrics`、`Trace`、`View`、`useChatStream({sessionId,onEvent})`、`adminFetch/getJSON/getSessionId`、`splitSSEFrames` 跨任务一致；`MetadataChips` 统一放 `MetadataChips.tsx`（Task6 Step1 注记已纠正 import 路径）；ChatView/DashboardView 先占位(Task5)后替换(Task6/7)，避免编译断裂。
- **风险**：npm 依赖版本以 `^` 容许小版本；若 happy-dom/vitest 版本解析失败，允许安装时接受 npm 选定的兼容版本（不改测试语义）。构建产物入库，保证「拉下来即跑」。
