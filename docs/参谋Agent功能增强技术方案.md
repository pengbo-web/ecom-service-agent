# 参谋 Agent 功能增强技术方案

## 一、背景与目标

### 现状

参谋 Agent（analyst）在架构中定位为"店铺经营参谋"，拥有五个只读工具：

| 工具 | 功能 |
|------|------|
| `shop_overview` | 订单量、GMV、客单价、退款率、取消率、咨询数 |
| `product_diagnostics` | 按 SKU 的下单量、销售额、退款率、退款原因 top3、库存 |
| `service_quality` | 按 skill 的成功率、工具失败率、转人工率 |
| `anomaly_scan` | 按确定性阈值扫出跨线异常 |
| `review_insights` | 全店均分、差评率、差评 top 商品与关键词 |

已有基础能力但存在 **四个核心缺口**：

1. **无专属 Skill 工作流**：增长 Agent 有 `draft-outreach-campaign` 完整流程 Skill，参谋没有对等的结构化流程（经营日报/商品体检/退货归因）
2. **无结构化报告输出**：参谋对话只能返回纯文本，无法渲染表格、指标卡等富内容
3. **无主动推送**：异常发现只在被动扫描时可见，不会主动通知店主
4. **前端对话入口过于简陋**：当前"问参谋"嵌在经营诊断页底部（`OperationsView.tsx` 的"diag"tab 最后一个 section），只是一个 input + send 按钮，无独立对话体验

### 目标

让参谋 Agent 成为店主可以**对话式交互**的经营助手，具备：
- 独立的对话入口（侧栏 tab）
- 3 个结构化 Skill 工作流（经营日报、商品体检、退货归因）
- 流式 SSE 回复（与买家侧同等体验）
- Markdown 富文本渲染（表格、列表、状态标记）
- 异常主动通知（轮询方案）

---

## 二、改动概览

```
后端:
  ① app/agent/skills/definitions/   — 新增 3 个参谋 Skill 定义
  ② app/multi_agent/agents.py       — 验证（已含 load_skill，无需改动）
  ③ app/api/app.py                  — 新增 SSE 流式端点 /api/seller/stream + 通知端点
  ④ app/api/streaming.py            — 新增 run_seller_streaming()
  ⑤ prompts/seller_profiles/analyst.md — 追加 Skill 加载指引
  ⑥ app/db.py                       — 新增 seller_notifications 表
  ⑦ app/multi_agent/collab.py       — handle_signal() 写入通知

前端:
  ⑧ webui/src/components/AnalystChatView.tsx  — 全新独立对话组件
  ⑨ webui/src/hooks/useChatStream.ts         — 泛化 endpoint 参数
  ⑩ webui/src/components/AppShell.tsx         — 侧栏新增 tab 入口
  ⑪ webui/src/App.tsx                         — 路由注册
  ⑫ webui/src/lib/api.ts                      — 通知 API
```

---

## 三、后端：参谋 Skill 系统

### 3.1 新增 3 个参谋 Skill 文件

**路径规范**：`app/agent/skills/definitions/<skill-name>/SKILL.md`，与现有 `draft-outreach-campaign` 同级。每个 Skill 文件遵循项目已有的 YAML frontmatter + markdown body 格式（参见 `app/agent/skills/loader.py` 的 `SkillMeta` dataclass）。

#### ① `daily-business-report/SKILL.md`（经营日报）

```yaml
---
name: daily-business-report
actor: seller
description: 当店主询问今日/近期经营情况、要求经营总结、日报、周报时使用。
  指导参谋 Agent 完成完整的经营报告流程：拉取总览 → 商品诊断 → 异常扫描 →
  评价洞察 → 输出结构化报告。
  关键词：日报、周报、经营情况、最近怎么样、生意如何、总结、报告。
workflow:
  steps:
    - tool: shop_overview
      args: { window_days: "从用户问题推断,默认7" }
    - tool: product_diagnostics
      args: { window_days: "同上", top_n: 5 }
    - tool: anomaly_scan
      args: { window_days: "同上" }
    - tool: review_insights
      args: { window_days: "同上" }
---
```

**正文**（SKILL.md body）指导参谋按以下流程执行：

1. 调 `shop_overview` 获取核心指标（订单量/GMV/退款率/客单价）
2. 调 `product_diagnostics` 获取商品 top5 表现
3. 调 `anomaly_scan` 扫描异常
4. 调 `review_insights` 获取评价洞察
5. 综合输出结构化报告，按以下 Markdown 模板：

```markdown
## 经营日报（近 N 天）

### 核心指标
| 指标 | 数值 | 状态 |
|------|------|------|
| 订单量 | X | — |
| GMV | ¥X | — |
| 客单价 | ¥X | — |
| 退款率 | X% | ⚠️/✅ |

### 商品表现 Top 5
（按销售额排序，标注退款率异常的商品）

### 异常与风险
（逐条列出 anomaly_scan 发现的跨线异常，含当前值与告警线）

### 评价洞察
（均分、差评率、差评高频词）

### 建议
1. （具体到动作的建议）
2. ...
```

#### ② `product-health-check/SKILL.md`（商品体检）

```yaml
---
name: product-health-check
actor: seller
description: 当店主询问某个商品怎么样、商品体检、退货原因分析时使用。
  关键词：体检、商品怎么样、P001、这个品、退款原因、退货分析。
workflow:
  slots:
    subject:
      pattern: '^.+$'
      hint: '商品 ID 或名称，必须来自店主的提问或 product_diagnostics 返回的真实数据'
  guards:
    - tool: product_diagnostics
      requires_tools: []
      validate: []
      deny: '分析前必须先调 product_diagnostics 获取商品真实数据'
---
```

**正文**指导参谋执行：

1. 调 `product_diagnostics` 获取目标商品的详细数据
2. 如果退款率偏高，调 `review_insights` 获取差评关键词
3. 如果涉及服务质量问题，调 `service_quality` 查看相关 skill 表现
4. 输出结构化商品体检报告：

```markdown
## 商品体检报告：[商品名]

### 基本数据
- 下单量：X | 销售额：¥X | 库存：X
- 退款率：X%（告警线 X%）

### 退款原因 Top 3
1. 原因A（X 次）
2. 原因B（X 次）
3. 原因C（X 次）

### 差评关键词
- 关键词1、关键词2...

### 归因与建议
1. （针对退款原因的具体改进建议）
2. ...
```

#### ③ `refund-attribution/SKILL.md`（退货归因）

```yaml
---
name: refund-attribution
actor: seller
description: 当店主询问退款率为什么升高、退货原因、售后分析时使用。
  关键词：退款、退货、退款率、售后、为什么退、退货原因。
workflow:
  guards:
    - tool: anomaly_scan
      requires_tools: []
      validate: []
      deny: '归因前必须先扫描异常，确认退款率是否真的异常'
---
```

**正文**指导参谋执行：

1. 调 `anomaly_scan` 确认退款率是否跨线
2. 调 `product_diagnostics` 定位退款集中的商品
3. 调 `review_insights` 获取差评关键词辅助归因
4. 调 `service_quality` 检查是否因服务流程导致退款
5. 输出归因报告（含因果链：异常现象 → 定位数据 → 根因判断 → 改进建议）

### 3.2 analyst 工具集验证（无需改动）

**文件**：`app/multi_agent/agents.py`

已确认 `_SELLER_COMMON_TOOLS` 包含 `load_skill` 和 `read_skill_file`：

```python
_SELLER_COMMON_TOOLS = {
    "search_knowledge", "load_skill", "read_skill_file", "read_tool_result",
}
```

analyst 的工具集 `_SELLER_COMMON_TOOLS | {"shop_overview", "product_diagnostics", "service_quality", "anomaly_scan", "review_insights"}` 已具备 Skill 加载能力。**此项无需改动。**

### 3.3 参谋 prompt 增强

**文件**：`prompts/seller_profiles/analyst.md`

在现有 prompt 末尾追加 Skill 加载指引段落：

```markdown
## Skill 工作流
当店主提出以下类型的问题时，**第一步就 `load_skill`**，按流程走：
- 经营情况/日报/周报/总结 → `load_skill(skill_name="daily-business-report")`
- 商品体检/某个商品怎么样/退款原因 → `load_skill(skill_name="product-health-check")`
- 退款率为什么高/退货分析/售后分析 → `load_skill(skill_name="refund-attribution")`

不要在没加载 Skill 的情况下直接调底层工具——Skill 封装了"先确认异常 → 再定位原因 → 最后给建议"的完整流程，直接调工具会漏掉前置判断。
```

---

## 四、后端：SSE 流式端点

### 4.1 现状分析

当前 `/api/seller/chat` 是**同步阻塞**的：

```python
# app/api/app.py 现有代码
result = orch.chat(req.message or "")
# ... 等完整回复后一次性返回
return {"success": True, "reply": reply, ...}
```

店主发消息后要干等十几秒（参谋需依次调 4-5 个工具），期间前端无任何反馈。

### 4.2 新增 `/api/seller/stream`

**文件**：`app/api/app.py`（在现有 seller 端点附近新增）

```python
@app.post("/api/seller/stream", dependencies=[Depends(admin_auth)])
async def seller_stream(req: SellerChatRequest):
    """店主与参谋的 SSE 流式对话。

    与买家侧 run_agent_streaming() 同一套事件协议（progress/stage/
    tool_call/reply/metadata），前端可复用同一 SSE 解析器。

    鉴权、会话管理与 /api/seller/chat 一致：admin_auth 门控、
    seller_sessions 独立 SessionManager、get_lock 防并发。
    """
    _require_seller_console()
    sid = (req.session_id or "").strip() or "seller-default"
    orch = seller_sessions.get_or_create(sid, user_id="seller")
    lock = seller_sessions.get_lock(sid)
    return StreamingResponse(
        run_seller_streaming(orch, req.message or "", sid, lock),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

**请求体**复用 `SellerChatRequest(session_id, message)`，无需新增 schema。

### 4.3 新增 `run_seller_streaming()`

**文件**：`app/api/streaming.py`（在现有 `run_agent_streaming` 之后新增）

与买家侧 `run_agent_streaming()` 平行，但**大幅简化**。核心复用 `queue.Queue` 桥接模式（`_SENTINEL` 已在模块级定义）：

```python
def run_seller_streaming(orch, message: str, session_id: str, lock) -> Iterator[dict]:
    """卖家侧 SSE 流式对话。

    复用买家侧的 queue-bridge 模式：orch.chat() 在子线程阻塞执行，
    通过 event_sink 回调将事件推入队列，主线程从队列消费并 yield SSE 帧。

    与买家侧 run_agent_streaming() 的差异（均为简化项）：
    - 无 guard_pipeline（店主是管理者，不需要输出护栏）
    - 无 consent/confirm 流程（参谋只做只读分析，不触发风险动作）
    - 无 HITL 转人工（店主不需要转接自己）
    - 无增量脱敏（经营数据不含买家 PII）
    """
    q: queue.Queue = queue.Queue()

    def sink(ev: dict) -> None:
        """event_sink 回调：引擎执行期间的 progress/stage/tool_call 等事件
        经此推入队列。reply 和 metadata 由 _run() 在 chat() 返回后显式推入。"""
        q.put(ev)

    def _run():
        with lock:
            orch.event_sink = sink
            try:
                result = orch.chat(message)
                # SellerOrchestrator.chat() 返回 CustomerServiceResponse pydantic 对象，
                # result.reply 是字符串（与现有 /api/seller/chat 同一读法）。
                q.put({"type": "reply", "content": result.reply})
                q.put({"type": "metadata",
                       "agent_key": getattr(orch, "last_agent_key", "analyst")})
            except Exception as exc:
                q.put({"type": "error", "message": str(exc)})
            finally:
                orch.event_sink = None
            try:
                orch.save()
            except Exception:  # noqa: BLE001 落盘失败不吞掉已生成的回复
                pass
        q.put(_SENTINEL)

    threading.Thread(target=_run, daemon=True).start()

    while True:
        ev = q.get()
        if ev is _SENTINEL:
            break
        yield ev
```

**事件协议**（前端据此渲染）：

| 事件类型 | 来源 | 前端用途 |
|----------|------|----------|
| `progress` | 引擎 `_emit_progress()` | 显示"正在分析/正在扫描..." |
| `stage` | 引擎 `_emit({"type": "stage", ...})` | 标记 react 循环开始/结束 |
| `route` | `SellerOrchestrator.chat()` | 显示"参谋-小策"或"增长-小拓"标签 |
| `tool_call` | 引擎 ReAct 循环 | 折叠显示工具调用详情 |
| `reply` | `_run()` 显式推入 | 最终回复全文 |
| `metadata` | `_run()` 显式推入 | `agent_key` 用于标签 |
| `error` | `_run()` 异常捕获 | 显示错误提示 |

---

## 五、前端：独立参谋对话界面

### 5.1 泛化 `useChatStream` hook

**文件**：`webui/src/hooks/useChatStream.ts`

当前 hook 硬编码 `fetch("/api/chat", ...)`。新增可选 `endpoint` 参数：

```typescript
export function useChatStream(opts: {
  sessionId: string;
  userId: string;
  currentItemId?: string;
  endpoint?: string;  // 新增：默认 "/api/chat"（买家），参谋传 "/api/seller/stream"
  onEvent: (e: SSEEvent) => void;
}) {
  const [streaming, setStreaming] = useState(false);
  const url = opts.endpoint || "/api/chat";

  const send = useCallback(async (message: string, confirm = false) => {
    // ... 与现有逻辑一致，仅 url 替换
    const resp = await fetch(url, { ... });
    // ...
  }, [opts, url]);

  return { send, streaming };
}
```

**兼容性**：买家侧 `ChatView.tsx` 不传 `endpoint`，行为完全不变。

**Pydantic 兼容性**：hook 发送的请求体包含 `user_id`/`confirm`/`current_item_id` 等额外字段，`SellerChatRequest` 未设 `extra='forbid'`，Pydantic v2 默认静默忽略额外字段，不会报错。

### 5.2 新增 `AnalystChatView.tsx`

**路径**：`webui/src/components/AnalystChatView.tsx`

独立的对话页面（不是嵌在经营诊断里的一个 section），参考 `ChatView.tsx` 的交互模式但面向店主。

#### 组件结构

```
AnalystChatView
├── 左侧：快捷操作面板（w-60, border-r）
│   ├── 快捷提问按钮组
│   │   ├── "📊 经营日报"  → 发送 "帮我出一份经营日报"
│   │   ├── "🏥 商品体检"  → 发送 "帮我做一次商品体检"
│   │   ├── "📉 退货归因"  → 发送 "退款率为什么升高了"
│   │   └── "🔍 异常扫描"  → 发送 "帮我扫描一下当前异常"
│   └── 最近异常摘要（从 /api/seller/overview 拉取 anomalies）
│       点击异常项 → 发送 "帮我分析一下{subject_name}的{kind}异常"
├── 右侧：对话区（flex-1）
│   ├── 通知条（顶部，条件渲染）
│   │   🔔 N 条新通知 [查看]
│   ├── 消息列表（ScrollArea, flex-1）
│   │   ├── UserBubble → MessageBubble(role="user")
│   │   ├── AssistantBubble → AgentBadge + MessageBubble(role="assistant")
│   │   │   └── 复用 react-markdown + remark-gfm 渲染表格/列表
│   │   ├── ToolCallPanel → <details> 折叠显示工具调用
│   │   └── ProgressIndicator → "正在拉取经营数据..."
│   └── 输入区（input + send button, Enter 发送）
```

#### 关键实现细节

1. **SSE 事件处理**：

```typescript
// onEvent 回调
const onEvent = useCallback((e: SSEEvent) => {
  switch (e.type) {
    case "route":        // 显示本轮 agent 标签
      setCurrentAgent(e.agent); break;
    case "progress":     // 显示进度文字
      setProgress(e.message || e.stage); break;
    case "tool_call":    // 折叠显示工具调用
      setTools(t => [...t, { name: e.tool, args: e.args }]); break;
    case "reply_delta":  // 流式追加回复
      setReply(r => r + (e.content || "")); break;
    case "reply":        // 终帧：覆盖为完整回复
      setReply(e.content); break;
    case "metadata":     // 记录 agent_key
      setAgentKey(e.agent_key); break;
    case "error":        // 显示错误
      setErr(e.message); break;
  }
}, []);
```

2. **Markdown 渲染**：复用已有 `MessageBubble` 组件（内部使用 `react-markdown` + `remark-gfm`，已支持表格/列表/代码块渲染）。

3. **会话 ID**：使用 `seller-` 前缀的独立 session_id（`sellerSessionRef = useRef("seller-" + Date.now())`），与买家会话命名空间隔离。

4. **通知轮询**：`useEffect` 每 30s 轮询 `/api/seller/notifications?unread_only=true`，更新 `unreadCount`。

### 5.3 AppShell 侧栏入口

**文件**：`webui/src/components/AppShell.tsx`

1. `View` 类型扩展：在现有联合类型中追加 `"analyst-chat"`
2. "经营侧"分组中新增 tab：

```typescript
{
  title: "经营侧",
  tabs: [
    { v: "analyst-chat", icon: <MessageSquare className="h-4 w-4" />,
      label: "参谋对话", hint: "与经营参谋对话式交互" },
    { v: "ops", icon: <BarChart3 className="h-4 w-4" />,
      label: "经营控制台", hint: "诊断 / 商机 / 触达审批" },
    { v: "collab", icon: <Network className="h-4 w-4" />,
      label: "多智能体协作", hint: "协作链时间线与编排规则" },
  ],
}
```

### 5.4 App.tsx 路由注册

**文件**：`webui/src/App.tsx`

```typescript
import { AnalystChatView } from "@/components/AnalystChatView";

// 渲染分支（在现有 view 条件链中追加）
{view === "analyst-chat" && (
  <AnalystChatView sessionId={`seller-${userId}`} />
)}
```

---

## 六、异常主动推送（通知系统）

### 6.1 设计思路

**不做 WebSocket 推送**（架构复杂度高，与项目 fail-soft 风格不符），采用**轮询 + 未读提示**方案：

1. 协作 Worker 的 `handle_signal()` 发现异常 → 写入 `seller_notifications` 表
2. 前端定时轮询 `GET /api/seller/notifications`（30s 间隔）
3. 参谋对话界面顶部显示未读通知角标
4. 点击通知自动发送相关问题到对话

### 6.2 后端通知表

**文件**：`app/db.py`（在现有 `Database` 类中新增表和方法）

```sql
CREATE TABLE IF NOT EXISTS seller_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,          -- anomaly/diagnosis/report
    title TEXT NOT NULL,         -- 通知标题（含 emoji 严重度标记）
    summary TEXT NOT NULL,       -- 摘要（诊断结论前 200 字）
    severity TEXT DEFAULT 'info',-- info/warning/critical
    read INTEGER DEFAULT 0,     -- 0=未读, 1=已读
    suggested_question TEXT,    -- 点击后自动填入对话输入框的问题
    created_at TEXT DEFAULT (datetime('now'))
);
```

**新增方法**：
- `add_notification(kind, title, summary, severity, suggested_question) -> int`
- `list_notifications(unread_only=False, limit=20) -> list[dict]`
- `mark_notification_read(nid: int) -> bool`
- `count_unread_notifications() -> int`

### 6.3 后端通知端点

**文件**：`app/api/app.py`（在 seller 端点区域内新增）

```python
@app.get("/api/seller/notifications", dependencies=[Depends(admin_auth)])
def seller_notifications(unread_only: bool = False, limit: int = 20):
    """获取店主通知列表。"""
    db = get_db()
    items = db.list_notifications(unread_only=unread_only, limit=limit)
    return {"notifications": items,
            "unread_count": db.count_unread_notifications()}

@app.post("/api/seller/notifications/{nid}/read", dependencies=[Depends(admin_auth)])
def mark_notification_read(nid: int):
    """标记单条通知已读。"""
    get_db().mark_notification_read(nid)
    return {"success": True}

@app.post("/api/seller/notifications/read-all", dependencies=[Depends(admin_auth)])
def mark_all_notifications_read():
    """一键全部已读。"""
    db = get_db()
    for n in db.list_notifications(unread_only=True):
        db.mark_notification_read(n["id"])
    return {"success": True}
```

### 6.4 通知写入时机

**文件**：`app/multi_agent/collab.py` 的 `handle_signal()` 中

在参谋 Agent 生成诊断后（`share()` 写入 `shared_context` 之后），同步写入通知：

```python
# handle_signal() 内，diagnosis 写入 shared_context 之后
try:
    from app.db import get_db
    severity = "warning" if anomaly.get("value", 0) > anomaly.get("threshold", 1) * 1.5 else "info"
    get_db().add_notification(
        kind="diagnosis",
        title=f"{'🔴' if severity == 'warning' else '⚠️'} {anomaly.get('kind', '')} 异常：{anomaly.get('subject_name', '')}",
        summary=diagnosis_text[:200] if diagnosis_text else "",
        severity=severity,
        suggested_question=f"帮我分析一下{anomaly.get('subject_name', '')}的{anomaly.get('kind', '')}异常",
    )
except Exception:  # noqa: BLE001 通知写入失败不影响诊断主流程
    pass
```

### 6.5 前端通知组件

集成在 `AnalystChatView.tsx` 中：

```tsx
// 轮询 hook
useEffect(() => {
  const timer = setInterval(async () => {
    try {
      const data = await adminFetch("/api/seller/notifications?unread_only=true&limit=5").then(r => r.json());
      setUnreadCount(data.unread_count || 0);
      setNotifications(data.notifications || []);
    } catch { /* 静默 */ }
  }, 30_000);
  return () => clearInterval(timer);
}, []);

// 渲染
{unreadCount > 0 && (
  <div className="mx-4 mt-2 rounded-md bg-amber-500/10 border border-amber-500/40 p-2 text-sm
                  flex items-center gap-2">
    <span>🔔 {unreadCount} 条新通知</span>
    <button onClick={() => setNotifOpen(!notifOpen)} className="text-xs underline">
      {notifOpen ? "收起" : "查看"}
    </button>
  </div>
)}
{notifOpen && notifications.map(n => (
  <div key={n.id} className="mx-4 p-2 text-xs border-b cursor-pointer hover:bg-muted"
       onClick={() => { sendQuestion(n.suggested_question); markRead(n.id); }}>
    <span className="font-medium">{n.title}</span>
    <span className="text-muted-foreground ml-2">{n.summary}</span>
  </div>
))}
```

---

## 七、文件改动清单

### 后端改动

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `app/agent/skills/definitions/daily-business-report/SKILL.md` | **新增** | 经营日报 Skill |
| `app/agent/skills/definitions/product-health-check/SKILL.md` | **新增** | 商品体检 Skill |
| `app/agent/skills/definitions/refund-attribution/SKILL.md` | **新增** | 退货归因 Skill |
| `prompts/seller_profiles/analyst.md` | **修改** | 末尾追加 Skill 加载指引段落 |
| `app/multi_agent/agents.py` | **验证** | 已确认无需改动 |
| `app/api/app.py` | **新增** | `/api/seller/stream` SSE 端点 + 3 个通知端点 |
| `app/api/streaming.py` | **新增** | `run_seller_streaming()` 函数（~40 行） |
| `app/db.py` | **新增** | `seller_notifications` 表 + 4 个 CRUD 方法 |
| `app/multi_agent/collab.py` | **修改** | `handle_signal()` 中新增通知写入（~10 行） |

### 前端改动

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `webui/src/components/AnalystChatView.tsx` | **新增** | 独立参谋对话页面（~350 行） |
| `webui/src/hooks/useChatStream.ts` | **修改** | 新增 `endpoint` 参数（3 行改动） |
| `webui/src/components/AppShell.tsx` | **修改** | View 类型 + GROUPS 新增 tab |
| `webui/src/App.tsx` | **修改** | import + 渲染分支 |
| `webui/src/lib/api.ts` | **新增** | `getSellerNotifications()` / `markNotificationRead()` |

---

## 八、实施顺序

### Phase 1：后端基础（先做，前端依赖）

| 步骤 | 文件 | 操作 |
|------|------|------|
| 1 | `app/agent/skills/definitions/daily-business-report/SKILL.md` | 创建经营日报 Skill |
| 2 | `app/agent/skills/definitions/product-health-check/SKILL.md` | 创建商品体检 Skill |
| 3 | `app/agent/skills/definitions/refund-attribution/SKILL.md` | 创建退货归因 Skill |
| 4 | `prompts/seller_profiles/analyst.md` | 追加 Skill 加载指引 |
| 5 | `app/api/streaming.py` | 新增 `run_seller_streaming()` |
| 6 | `app/api/app.py` | 新增 `/api/seller/stream` 端点 |

### Phase 2：前端对话界面

| 步骤 | 文件 | 操作 |
|------|------|------|
| 7 | `webui/src/hooks/useChatStream.ts` | 泛化 `endpoint` 参数 |
| 8 | `webui/src/components/AnalystChatView.tsx` | 创建独立对话组件 |
| 9 | `webui/src/components/AppShell.tsx` | 添加侧栏 tab |
| 10 | `webui/src/App.tsx` | 注册路由 |

### Phase 3：通知系统

| 步骤 | 文件 | 操作 |
|------|------|------|
| 11 | `app/db.py` | 新增通知表 + CRUD |
| 12 | `app/multi_agent/collab.py` | `handle_signal()` 写入通知 |
| 13 | `app/api/app.py` | 通知端点 |
| 14 | `webui/src/lib/api.ts` | 通知 API 客户端 |
| 15 | `webui/src/components/AnalystChatView.tsx` | 集成通知轮询与 UI |

---

## 九、验证方案

### 端到端测试路径

1. **启动后端**：`python main.py`
2. **启动前端**：`cd webui && npm run dev`
3. **进入参谋对话页面**：点击侧栏"参谋对话" tab
4. **测试经营日报**：点击快捷按钮"📊 经营日报"或输入"帮我出一份经营日报"
   - ✅ 验证：参谋依次调用 shop_overview → product_diagnostics → anomaly_scan → review_insights
   - ✅ 验证：前端正确渲染 Markdown 表格、指标、状态标记
   - ✅ 验证：回复逐段流出（SSE），而非一次性返回
5. **测试商品体检**：输入"帮我看看 P001 这个商品"
   - ✅ 验证：加载 product-health-check Skill，输出商品体检报告
6. **测试退货归因**：输入"退款率为什么升高了"
   - ✅ 验证：加载 refund-attribution Skill，先 anomaly_scan 再归因
7. **测试 Agent 标签**：验证每条回复标注"参谋-小策"或"增长-小拓"
8. **测试异常通知**：在协作 Worker 运行中触发异常，验证通知出现在参谋对话页面顶部
9. **测试通知跳转**：点击通知，验证自动发送建议问题到对话

### 关键断言

- Skill 加载后工具调用顺序符合 workflow 定义（guard 阻止跳步）
- 结构化报告包含表格 + 指标 + 建议三部分
- 前端 SSE 事件流不丢帧、不重复渲染
- 通知点击后自动填充建议问题到输入框并发送
- 参谋工具全只读，对话中不产生任何写操作
