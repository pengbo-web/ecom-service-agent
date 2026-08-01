# 客服工作台(坐席侧)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为现有智能客服增加一个企业级「客服工作台」:一名坐席在一个界面里同时看到多个客户的实时会话、一键人工接管某路会话并以人工身份回复,客户侧即时收到——直观呈现"多用户并发接待"。

**Architecture:** 后端已具备多用户并发能力(SessionManager 按 `session_id+user_id` 隔离、每会话独立锁、HITL 人工模式短路、会话表持久化)。本方案**只补三层缺口**:①跨用户的只读聚合接口(列所有会话 / 读任意会话消息);②人工回复注入接口(把坐席消息写进该会话的 `raw_messages` 并落盘,置该会话为人工模式);③客户侧轮询接收人工回复。前端把单薄的「坐席」Tab 重写为**三栏工作台**(会话列表 | 消息流 | 客户上下文),视觉对标企业级客服台(头像、状态色、未读、相对时间、AI/人工态)。多客户演示靠 `?user=` 参数让不同窗口以不同客户进入。

**Tech Stack:** 后端 FastAPI + 现有 `SessionManager`/`HitlManager`/`get_db()`(SQLite)/`reconstruct_bubbles`;前端 React 18 + Vite + TypeScript + Tailwind + shadcn/ui(全部已在用)。不引入任何新依赖。

## Global Constraints

- 后端:Python 3.11+,不新增第三方依赖;所有管理接口挂 `Depends(admin_auth)`(与现有 `/api/handoffs` 等一致;`ADMIN_TOKEN` 为空时自动放行,见 `app/hardening/auth.py`)。
- 后端:所有对 `raw_messages` 的读写必须在 `session_lock.guard(session_id)` 内,与 `/api/chat`、`consolidate` 互斥(避免边写边读损坏历史)。
- 人工回复消息格式必须与 `reconstruct_bubbles`(`app/api/history.py`)兼容:`role="assistant"`、`content` 为 JSON 且含 `"reply"` 字段,否则不会渲染成气泡。
- 前端:复用现有 `adminFetch`(自动带 `X-Admin-Token`)、shadcn `Card/Button/Badge/ScrollArea/Input`、现有 `MessageBubble`;不新增 UI 库。
- 前端:所有中文文案与现有风格一致;`.ps1` 类文件不涉及(本方案不改脚本)。
- 会话消息只对**热会话**(在 `SessionManager` 中)可读;冷会话回退冷快照(与现有 `session_history` 一致)。
- demo 注入必须仅对 demo 客户(`settings.demo_hmdp_user_id`)生效,否则多窗口会全部塌缩成同一个 hmdp 身份。

---

## File Structure

**后端(改动集中在 3 个文件):**
- `app/db/database.py` — 新增 `list_all_conversations(limit)`:跨用户列会话。
- `app/api/app.py` — 新增 3 个管理端点(`/api/admin/conversations`、`/api/admin/session/{id}/messages`、`/api/admin/session/{id}/reply`);修 `/api/chat` demo 注入条件。
- `app/api/hmdp_identity.py` — 已有 `seed_demo_hmdp_identity`,无需改;第二个 demo 客户由启动播种循环覆盖(见 Task 4)。

**前端(新增 workbench 目录,拆小文件):**
- `webui/src/lib/api.ts` — 新增工作台 API 函数 + 类型。
- `webui/src/components/workbench/ConversationList.tsx` — 左栏:所有客户会话列表。
- `webui/src/components/workbench/MessageThread.tsx` — 中栏:选中会话的消息流 + 人工回复框。
- `webui/src/components/workbench/ContextPanel.tsx` — 右栏:客户上下文(身份/会话状态/接管开关)。
- `webui/src/components/workbench/parts.ts` — 小工具:头像色、相对时间、状态映射(DRY,三个组件共用)。
- `webui/src/components/WorkbenchView.tsx` — 组装三栏 + 轮询刷新 + 选中态。
- `webui/src/components/AppShell.tsx` — 「坐席」Tab 文案改「工作台」。
- `webui/src/App.tsx` — `seat` 视图渲染 `WorkbenchView`;支持 `?user=` 多客户进入。
- `webui/src/components/ChatView.tsx` — 客户侧轮询 history 接收人工回复。

---

## Task 1: 后端 — 跨用户列会话 `list_all_conversations` + `/api/admin/conversations`

**Files:**
- Modify: `app/db/database.py`(在 `list_conversations` 之后新增方法)
- Modify: `app/api/app.py`(在 `/api/handoffs` 端点附近新增端点)
- Test: `tests/test_workbench_api.py`(新建)

**Interfaces:**
- Produces:
  - `Database.list_all_conversations(limit: int = 50) -> list[dict]`,每项含 `conversation_id, user_id, status, created_at`(open 优先、再按 created_at 倒序)。
  - `GET /api/admin/conversations` → `{"conversations": [{conversation_id, user_id, status, created_at, manual: bool, preview: str, turns: int}]}`。`manual` 来自 `hitl.manual_mode.is_manual`;`preview`/`turns` 来自热会话 `manager.peek_messages` 的最后一条 user 文本与用户消息条数(冷会话为 `""`/`0`)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_workbench_api.py
from fastapi.testclient import TestClient
from app.api.app import create_app
from app.api.session_manager import SessionManager


def _client():
    app = create_app(session_manager=SessionManager())
    return TestClient(app)


def test_admin_conversations_lists_across_users():
    c = _client()
    # 两个不同客户各开一个会话
    c.post("/api/users", json={"user_id": "alice", "name": "Alice"})
    c.post("/api/users", json={"user_id": "bob", "name": "Bob"})
    r1 = c.post("/api/conversation/open", json={"user_id": "alice"},
                headers={"Authorization": "Bearer " + c.post("/api/auth/login", json={"user_id": "alice"}).json()["token"]})
    r2 = c.post("/api/conversation/open", json={"user_id": "bob"},
                headers={"Authorization": "Bearer " + c.post("/api/auth/login", json={"user_id": "bob"}).json()["token"]})
    assert r1.status_code == 200 and r2.status_code == 200

    resp = c.get("/api/admin/conversations")
    assert resp.status_code == 200
    users = {row["user_id"] for row in resp.json()["conversations"]}
    assert {"alice", "bob"} <= users
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_workbench_api.py::test_admin_conversations_lists_across_users -v`
Expected: FAIL(404,端点未定义)

- [ ] **Step 3: 加 DB 方法**

在 `app/db/database.py` 的 `list_conversations` 方法后新增:

```python
    def list_all_conversations(self, limit: int = 50) -> list[dict]:
        """跨用户列会话:进行中(open)优先,再按创建时间倒序。供坐席工作台聚合。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM conversations "
                "ORDER BY (status='open') DESC, created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
```

- [ ] **Step 4: 加端点**

在 `app/api/app.py` 的 `handoffs()` 端点(`@app.get("/api/handoffs"...)`)之前新增:

```python
    @app.get("/api/admin/conversations", dependencies=[Depends(admin_auth)])
    def admin_conversations(limit: int = 50):
        """坐席工作台:列出所有客户的会话(跨用户)+ 人工态 + 预览。"""
        out = []
        for c in get_db().list_all_conversations(limit=limit):
            sid = c["conversation_id"]
            msgs = manager.peek_messages(sid) or []
            user_msgs = [m for m in msgs if m.get("role") == "user"]
            preview = user_msgs[-1]["content"] if user_msgs else ""
            out.append({
                "conversation_id": sid,
                "user_id": c.get("user_id"),
                "status": c.get("status"),
                "created_at": c.get("created_at"),
                "manual": bool(hitl and hitl.manual_mode.is_manual(sid)),
                "preview": preview[:60],
                "turns": len(user_msgs),
            })
        return {"conversations": out}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_workbench_api.py::test_admin_conversations_lists_across_users -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/db/database.py app/api/app.py tests/test_workbench_api.py
git commit -m "feat(workbench): 跨用户列会话接口 /api/admin/conversations"
```

---

## Task 2: 后端 — 坐席读任意会话消息 `/api/admin/session/{id}/messages`

**Files:**
- Modify: `app/api/app.py`
- Test: `tests/test_workbench_api.py`

**Interfaces:**
- Consumes: `manager.peek_messages`(Task 1 已用)、`reconstruct_bubbles`(`app/api/history.py`)、`get_db().get_session_snapshot`。
- Produces: `GET /api/admin/session/{session_id}/messages` → `{"session_id": str, "turns": [{"role":"user"|"assistant","content":str}]}`。不做归属校验(admin 网关已控),热存储空则回退冷快照。

- [ ] **Step 1: 写失败测试**

```python
def test_admin_read_any_session_messages():
    c = _client()
    tok = c.post("/api/auth/login", json={"user_id": "alice"}).json  # placeholder replaced below
```

改为完整测试(替换上面占位):

```python
def test_admin_read_any_session_messages():
    c = _client()
    c.post("/api/users", json={"user_id": "carol", "name": "Carol"})
    tok = c.post("/api/auth/login", json={"user_id": "carol"}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    sid = c.post("/api/conversation/open", json={"user_id": "carol"}, headers=h).json()["conversation_id"]
    # 坐席可读该会话消息(即便自身非该客户),返回结构正确
    r = c.get(f"/api/admin/session/{sid}/messages")
    assert r.status_code == 200
    assert r.json()["session_id"] == sid
    assert isinstance(r.json()["turns"], list)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_workbench_api.py::test_admin_read_any_session_messages -v`
Expected: FAIL(404)

- [ ] **Step 3: 加端点**

在 `app/api/app.py` 的 `admin_conversations` 之后新增:

```python
    @app.get("/api/admin/session/{session_id}/messages", dependencies=[Depends(admin_auth)])
    def admin_session_messages(session_id: str):
        """坐席读任意会话的气泡(管理网关已控权限,不做客户归属校验)。"""
        from app.api.history import reconstruct_bubbles
        messages = manager.peek_messages(session_id)
        if not messages and settings.session_snapshot_enabled:
            try:
                snap = get_db().get_session_snapshot(session_id)
                if snap:
                    messages = snap.get("messages") or []
            except Exception:  # noqa: BLE001
                messages = messages
        return {"session_id": session_id, "turns": reconstruct_bubbles(messages)}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_workbench_api.py::test_admin_read_any_session_messages -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/api/app.py tests/test_workbench_api.py
git commit -m "feat(workbench): 坐席读任意会话消息接口"
```

---

## Task 3: 后端 — 人工回复注入 `/api/admin/session/{id}/reply`

**Files:**
- Modify: `app/api/app.py`
- Test: `tests/test_workbench_api.py`

**Interfaces:**
- Consumes: `manager.get_or_create(session_id, user_id)`、`session_lock.guard`、`hitl.manual_mode`。
- Produces: `POST /api/admin/session/{session_id}/reply`,body `{"text": str}` → `{"status":"ok","turns":[...]}`。行为:置该会话为人工模式(若尚非);把坐席回复以 `role="assistant"`、`content=json({"reply":text,"intent":"human_agent","confidence":1.0,"requires_human":false,"follow_up_question":null})` 追加进 `raw_messages` 并 `agent.save()`;全程持会话锁。返回追加后的最新气泡列表。

- [ ] **Step 1: 写失败测试**

```python
import json as _json


def test_admin_human_reply_appends_bubble_and_sets_manual():
    c = _client()
    c.post("/api/users", json={"user_id": "dave", "name": "Dave"})
    tok = c.post("/api/auth/login", json={"user_id": "dave"}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    sid = c.post("/api/conversation/open", json={"user_id": "dave"}, headers=h).json()["conversation_id"]

    r = c.post(f"/api/admin/session/{sid}/reply", json={"text": "您好,人工客服为您处理"})
    assert r.status_code == 200
    turns = r.json()["turns"]
    assert turns and turns[-1] == {"role": "assistant", "content": "您好,人工客服为您处理"}

    # 该会话已转人工:客户再发消息被短路(不调 AI)
    stream = c.post("/api/chat", json={"session_id": sid, "message": "在吗", "user_id": "dave"}, headers=h)
    assert stream.status_code == 200
    assert "人工客服" in stream.text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_workbench_api.py::test_admin_human_reply_appends_bubble_and_sets_manual -v`
Expected: FAIL(404)

- [ ] **Step 3: 加 schema(可选)与端点**

先在 `app/api/schemas.py` 顶部已有 schema 集合处新增(若偏好显式模型):

```python
class AgentReplyRequest(BaseModel):
    text: str = ""
```

在 `app/api/app.py` 的 `admin_session_messages` 之后新增端点(直接取 body,不强制引 schema 亦可):

```python
    @app.post("/api/admin/session/{session_id}/reply", dependencies=[Depends(admin_auth)])
    def admin_session_reply(session_id: str, req: "AgentReplyRequest"):
        """坐席以人工身份回复该会话:置人工模式 + 追加 assistant 气泡并落盘。"""
        text = (req.text or "").strip()
        if not text:
            raise HTTPException(422, "回复内容不能为空")
        conv = get_db().get_conversation(session_id)
        uid = (conv or {}).get("user_id") or "default"
        if hitl is not None and not hitl.manual_mode.is_manual(session_id):
            hitl.manual_mode.toggle(session_id)   # 回复即接管:转人工,AI 暂停
        agent = manager.get_or_create(session_id, uid)
        from app.api.history import reconstruct_bubbles
        with session_lock.guard(session_id) as got:
            if not got:
                raise HTTPException(409, "该会话正在处理中,请稍后再试")
            msgs = getattr(agent, "raw_messages", None)
            if isinstance(msgs, list):
                msgs.append({"role": "assistant", "content": json.dumps({
                    "intent": "human_agent", "confidence": 1.0, "reply": text,
                    "requires_human": False, "follow_up_question": None,
                }, ensure_ascii=False)})
                save = getattr(agent, "save", None)
                if callable(save):
                    try:
                        save()
                    except Exception:  # noqa: BLE001
                        pass
            bubbles = reconstruct_bubbles(getattr(agent, "raw_messages", []))
        return {"status": "ok", "turns": bubbles}
```

在 `app/api/app.py` 顶部 `from app.api.schemas import (...)` 追加 `AgentReplyRequest`(若采用显式 schema)。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_workbench_api.py::test_admin_human_reply_appends_bubble_and_sets_manual -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/api/app.py app/api/schemas.py tests/test_workbench_api.py
git commit -m "feat(workbench): 人工回复注入接口(转人工+落盘气泡)"
```

---

## Task 4: 后端 — 修 demo 注入(放开多客户)+ 播种第二个 demo 客户

**Files:**
- Modify: `app/api/app.py`(`/api/chat` 顶部 demo 注入分支)
- Modify: `app/api/app.py`(启动播种块)
- Test: `tests/test_workbench_api.py`

**Interfaces:**
- Consumes: `settings.demo_mode`、`settings.demo_hmdp_user_id`、`settings.demo_hmdp_token`。
- Produces:demo 注入**仅当** SPA 登录用户 == `demo_hmdp_user_id` 时生效;其它客户(alice/1011…)按自身身份聊,互不塌缩。第二个 demo 客户(hmdp id `1011`)也在启动时播种 token(供 `?user=1011` 体验真实数据)。

- [ ] **Step 1: 写失败测试**

```python
def test_demo_injection_only_targets_demo_user(monkeypatch):
    from app.config.settings import settings as S
    monkeypatch.setattr(S, "demo_mode", True)
    monkeypatch.setattr(S, "demo_hmdp_user_id", "1")
    c = _client()
    # 非 demo 客户 alice 聊天:不应被注入 hmdp_token 塌缩成 "1"
    c.post("/api/users", json={"user_id": "alice2", "name": "A"})
    tok = c.post("/api/auth/login", json={"user_id": "alice2"}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    sid = c.post("/api/conversation/open", json={"user_id": "alice2"}, headers=h).json()["conversation_id"]
    c.post("/api/chat", json={"session_id": sid, "message": "你好", "user_id": "alice2"}, headers=h)
    conv = c.get(f"/api/admin/conversations").json()["conversations"]
    # alice2 的会话仍归属 alice2(未被改写成 "1")
    assert any(row["user_id"] == "alice2" for row in conv)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_workbench_api.py::test_demo_injection_only_targets_demo_user -v`
Expected: FAIL(alice2 被注入 demo token → 归属塌缩成 "1",断言不成立)

- [ ] **Step 3: 收紧 demo 注入条件**

`app/api/app.py` 的 `/api/chat` 顶部,把:

```python
        if settings.demo_mode and not getattr(req, "hmdp_token", ""):
            req.hmdp_token = settings.demo_hmdp_token
```

改为:

```python
        # demo 模式:仅当当前登录用户就是 demo 客户时,才注入其 hmdp 身份 →
        # 聊真实订单。其它客户(?user=xxx)按自身身份聊,不塌缩成同一 hmdp 身份。
        if (settings.demo_mode and not getattr(req, "hmdp_token", "")
                and str(req.user_id) == str(settings.demo_hmdp_user_id)):
            req.hmdp_token = settings.demo_hmdp_token
```

- [ ] **Step 4: 播种第二个 demo 客户**

`app/api/app.py` 启动播种块,把单次播种改为循环(demo 主客户 + 备用客户 1011):

```python
    if settings.demo_mode:
        try:
            from app.api.hmdp_identity import seed_demo_hmdp_identity
            seed_demo_hmdp_identity(settings.demo_hmdp_token, settings.demo_hmdp_user_id,
                                    settings.demo_hmdp_nickname)
            # 第二个 demo 客户(hmdp id 1011,有 1 笔订单),供 ?user=1011 体验并发
            seed_demo_hmdp_identity("demo-hmdp-token-1011", "1011", "另一位顾客")
        except Exception:  # noqa: BLE001
            pass
```

> 注:`?user=1011` 走真实 hmdp 数据需在 Task 10 的注入分支加一个别名映射(见 Task 10 Step 3)。若只演示"多路并发"而不要求 1011 也有真实订单,本步可省略。

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_workbench_api.py::test_demo_injection_only_targets_demo_user -v`
Expected: PASS

- [ ] **Step 6: 全量后端回归**

Run: `.venv\Scripts\python.exe -m pytest tests/ -q`
Expected: 全绿(确认新改动不破坏既有测试)

- [ ] **Step 7: 提交**

```bash
git add app/api/app.py tests/test_workbench_api.py
git commit -m "feat(workbench): demo 注入仅对 demo 客户生效,放开多客户并发"
```

---

## Task 5: 前端 — 工作台 API 客户端 + 类型

**Files:**
- Modify: `webui/src/lib/api.ts`
- Test: `webui/src/tests/workbench-api.test.tsx`(新建)

**Interfaces:**
- Produces:
  - `type WbConversation = { conversation_id: string; user_id: string; status: string; created_at: string; manual: boolean; preview: string; turns: number }`
  - `type WbTurn = { role: "user" | "assistant"; content: string }`
  - `adminListConversations(limit?: number): Promise<WbConversation[]>`
  - `adminGetMessages(sessionId: string): Promise<WbTurn[]>`
  - `adminReply(sessionId: string, text: string): Promise<WbTurn[]>`
  - `adminTakeover(sessionId: string): Promise<{ mode: string }>`(复用现有 `/api/session/{id}/takeover`)

- [ ] **Step 1: 写失败测试**

```tsx
// webui/src/tests/workbench-api.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { adminListConversations } from "@/lib/api";

describe("workbench api", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => ({ conversations: [{ conversation_id: "c-1", user_id: "alice", status: "open", created_at: "", manual: false, preview: "hi", turns: 1 }] }),
    })));
    localStorage.clear();
  });
  it("parses conversation list", async () => {
    const list = await adminListConversations();
    expect(list[0].user_id).toBe("alice");
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd webui && npx vitest run src/tests/workbench-api.test.tsx`
Expected: FAIL(`adminListConversations` 未导出)

- [ ] **Step 3: 实现 API 函数**

在 `webui/src/lib/api.ts` 末尾新增:

```typescript
// ---- 客服工作台(坐席侧)----
export type WbConversation = {
  conversation_id: string; user_id: string; status: string;
  created_at: string; manual: boolean; preview: string; turns: number;
};
export type WbTurn = { role: "user" | "assistant"; content: string };

export async function adminListConversations(limit = 50): Promise<WbConversation[]> {
  const r = await adminFetch(`/api/admin/conversations?limit=${limit}`);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return (await r.json()).conversations as WbConversation[];
}
export async function adminGetMessages(sessionId: string): Promise<WbTurn[]> {
  const r = await adminFetch(`/api/admin/session/${sessionId}/messages`);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return (await r.json()).turns as WbTurn[];
}
export async function adminReply(sessionId: string, text: string): Promise<WbTurn[]> {
  const r = await adminFetch(`/api/admin/session/${sessionId}/reply`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return (await r.json()).turns as WbTurn[];
}
export async function adminTakeover(sessionId: string): Promise<{ mode: string }> {
  const r = await adminFetch(`/api/session/${sessionId}/takeover`, { method: "POST" });
  return r.json();
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd webui && npx vitest run src/tests/workbench-api.test.tsx`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add webui/src/lib/api.ts webui/src/tests/workbench-api.test.tsx
git commit -m "feat(workbench): 前端工作台 API 客户端"
```

---

## Task 6: 前端 — 共用工具 `parts.ts`(头像色/相对时间/状态)

**Files:**
- Create: `webui/src/components/workbench/parts.ts`
- Test: `webui/src/tests/workbench-parts.test.tsx`(新建)

**Interfaces:**
- Produces:
  - `avatarColor(seed: string): string`(稳定的 HSL 背景色,同一 seed 恒定)
  - `initials(name: string): string`(取 1-2 字作头像文字)
  - `relativeTime(iso: string): string`(“刚刚/N分钟前/N小时前/日期”)
  - `statusMeta(c: {status:string; manual:boolean}): { label: string; tone: "ai"|"manual"|"closed" }`

- [ ] **Step 1: 写失败测试**

```tsx
// webui/src/tests/workbench-parts.test.tsx
import { describe, it, expect } from "vitest";
import { initials, statusMeta, avatarColor } from "@/components/workbench/parts";

describe("workbench parts", () => {
  it("initials takes first chars", () => {
    expect(initials("小鱼同学")).toBe("小鱼");
    expect(initials("alice")).toBe("AL");
  });
  it("statusMeta maps manual/ai/closed", () => {
    expect(statusMeta({ status: "open", manual: true }).tone).toBe("manual");
    expect(statusMeta({ status: "open", manual: false }).tone).toBe("ai");
    expect(statusMeta({ status: "closed", manual: false }).tone).toBe("closed");
  });
  it("avatarColor is stable per seed", () => {
    expect(avatarColor("alice")).toBe(avatarColor("alice"));
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd webui && npx vitest run src/tests/workbench-parts.test.tsx`
Expected: FAIL(模块不存在)

- [ ] **Step 3: 实现 parts.ts**

```typescript
// webui/src/components/workbench/parts.ts
export function avatarColor(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) % 360;
  return `hsl(${h} 55% 55%)`;
}

export function initials(name: string): string {
  const s = (name || "?").trim();
  if (/^[\x00-\x7f]+$/.test(s)) return s.slice(0, 2).toUpperCase();  // 英文/数字取两位
  return s.slice(0, 2);                                             // 中文取两字
}

export function relativeTime(iso: string): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)}分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)}小时前`;
  return iso.slice(5, 16).replace("T", " ");
}

export function statusMeta(c: { status: string; manual: boolean }): {
  label: string; tone: "ai" | "manual" | "closed";
} {
  if (c.status !== "open") return { label: "已结束", tone: "closed" };
  if (c.manual) return { label: "人工中", tone: "manual" };
  return { label: "AI 接待", tone: "ai" };
}

export const TONE_CLASS: Record<"ai" | "manual" | "closed", string> = {
  ai: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  manual: "bg-blue-500/15 text-blue-600 dark:text-blue-400",
  closed: "bg-muted text-muted-foreground",
};
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd webui && npx vitest run src/tests/workbench-parts.test.tsx`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add webui/src/components/workbench/parts.ts webui/src/tests/workbench-parts.test.tsx
git commit -m "feat(workbench): 共用工具(头像色/相对时间/状态映射)"
```

---

## Task 7: 前端 — 会话列表 `ConversationList.tsx`(左栏)

**Files:**
- Create: `webui/src/components/workbench/ConversationList.tsx`

**Interfaces:**
- Consumes: `WbConversation`(Task 5)、`parts.ts`(Task 6)、shadcn `ScrollArea/Badge`。
- Produces: `ConversationList({ items, selected, onSelect, filter, onFilter })`,props:
  - `items: WbConversation[]`、`selected?: string`、`onSelect(id: string): void`
  - `filter: "all"|"manual"|"open"`、`onFilter(f): void`
  渲染:顶部过滤标签(全部/人工中/进行中)+ 每条会话卡(头像、客户名、预览、相对时间、状态徽标、轮次)。选中态高亮。

- [ ] **Step 1: 实现组件**

```tsx
// webui/src/components/workbench/ConversationList.tsx
import { ScrollArea } from "@/components/ui/scroll-area";
import { avatarColor, initials, relativeTime, statusMeta, TONE_CLASS } from "./parts";
import type { WbConversation } from "@/lib/api";

type Filter = "all" | "manual" | "open";

export function ConversationList({ items, selected, onSelect, filter, onFilter }: {
  items: WbConversation[]; selected?: string; onSelect: (id: string) => void;
  filter: Filter; onFilter: (f: Filter) => void;
}) {
  const shown = items.filter((c) =>
    filter === "all" ? true : filter === "manual" ? c.manual : c.status === "open");
  const tabs: { key: Filter; label: string }[] = [
    { key: "all", label: "全部" }, { key: "open", label: "进行中" }, { key: "manual", label: "人工中" },
  ];
  return (
    <div className="flex h-full flex-col border-r bg-card/40">
      <div className="flex items-center gap-1 border-b px-3 py-2">
        {tabs.map((t) => (
          <button key={t.key} onClick={() => onFilter(t.key)}
            className={`rounded-full px-3 py-1 text-xs transition ${
              filter === t.key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-secondary"}`}>
            {t.label}
          </button>
        ))}
        <span className="ml-auto text-xs text-muted-foreground">{shown.length} 路会话</span>
      </div>
      <ScrollArea className="flex-1">
        {shown.length === 0 ? (
          <div className="p-6 text-center text-xs text-muted-foreground">暂无会话</div>
        ) : (
          <ul className="flex flex-col">
            {shown.map((c) => {
              const sm = statusMeta(c);
              const active = c.conversation_id === selected;
              return (
                <li key={c.conversation_id}>
                  <button onClick={() => onSelect(c.conversation_id)}
                    className={`flex w-full items-start gap-3 border-b px-3 py-3 text-left transition ${
                      active ? "bg-secondary" : "hover:bg-secondary/50"}`}>
                    <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-xs font-medium text-white"
                      style={{ background: avatarColor(c.user_id) }}>
                      {initials(c.user_id)}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium">客户 {c.user_id}</span>
                        <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">{relativeTime(c.created_at)}</span>
                      </span>
                      <span className="mt-0.5 block truncate text-xs text-muted-foreground">
                        {c.preview || "（暂无消息）"}
                      </span>
                      <span className={`mt-1 inline-block rounded px-1.5 py-0.5 text-[11px] ${TONE_CLASS[sm.tone]}`}>
                        {sm.label}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </ScrollArea>
    </div>
  );
}
```

- [ ] **Step 2: 类型/构建自检**

Run: `cd webui && npx tsc -b`
Expected: 无错误(仅本文件相关)

- [ ] **Step 3: 提交**

```bash
git add webui/src/components/workbench/ConversationList.tsx
git commit -m "feat(workbench): 会话列表左栏(头像/预览/状态/过滤)"
```

---

## Task 8: 前端 — 消息流 + 人工回复 `MessageThread.tsx`(中栏)

**Files:**
- Create: `webui/src/components/workbench/MessageThread.tsx`

**Interfaces:**
- Consumes: `WbTurn`、`adminReply`、`adminTakeover`(Task 5)、现有 `MessageBubble`、shadcn `Button/ScrollArea/Textarea`(若无 Textarea 用原生 `<textarea>`)。
- Produces: `MessageThread({ sessionId, userId, manual, turns, onAfterReply, onToggleManual })`:
  - 顶部:客户名 + 人工/自动开关(调 `onToggleManual`)。
  - 中部:消息气泡(user 左、assistant 右;`intent==human_agent` 无从 turn 区分,统一 assistant 样式即可)。
  - 底部:人工回复输入框 + 发送(仅人工模式可用;发送后调 `adminReply`,成功回调 `onAfterReply(turns)`)。

- [ ] **Step 1: 实现组件**

```tsx
// webui/src/components/workbench/MessageThread.tsx
import { useState } from "react";
import { MessageBubble } from "@/components/MessageBubble";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { adminReply, type WbTurn } from "@/lib/api";

export function MessageThread({ sessionId, userId, manual, turns, onAfterReply, onToggleManual }: {
  sessionId: string; userId: string; manual: boolean; turns: WbTurn[];
  onAfterReply: (turns: WbTurn[]) => void; onToggleManual: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  async function send() {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    try {
      const next = await adminReply(sessionId, text);
      setDraft("");
      onAfterReply(next);
    } finally { setBusy(false); }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b px-4 py-2.5">
        <span className="text-sm font-semibold">客户 {userId}</span>
        <span className="font-mono text-[11px] text-muted-foreground">{sessionId}</span>
        <Button size="sm" variant={manual ? "default" : "secondary"} className="ml-auto h-7 text-xs"
          onClick={onToggleManual}>
          {manual ? "● 人工接管中(点击交还 AI)" : "转人工接管"}
        </Button>
      </div>

      <ScrollArea className="flex-1">
        <div className="mx-auto flex max-w-2xl flex-col gap-3 p-5">
          {turns.length === 0 ? (
            <div className="mt-16 text-center text-sm text-muted-foreground">该会话暂无消息</div>
          ) : turns.map((t, i) => (
            <MessageBubble key={i} role={t.role}>{t.content}</MessageBubble>
          ))}
        </div>
      </ScrollArea>

      <div className="border-t p-3">
        {!manual && (
          <div className="mb-2 rounded-md bg-amber-500/10 px-3 py-1.5 text-xs text-amber-600 dark:text-amber-400">
            当前为 AI 自动接待。点右上「转人工接管」后即可以人工身份回复。
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            className="min-h-[44px] flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm"
            placeholder={manual ? "输入人工回复,Enter 发送…" : "接管后可回复"}
            disabled={!manual || busy}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
          />
          <Button size="sm" disabled={!manual || busy || !draft.trim()} onClick={send}>
            {busy ? "发送中…" : "发送"}
          </Button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: 类型/构建自检**

Run: `cd webui && npx tsc -b`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
git add webui/src/components/workbench/MessageThread.tsx
git commit -m "feat(workbench): 消息流中栏 + 人工回复框"
```

---

## Task 9: 前端 — 客户上下文 `ContextPanel.tsx`(右栏)

**Files:**
- Create: `webui/src/components/workbench/ContextPanel.tsx`

**Interfaces:**
- Consumes: `WbConversation`、`parts.ts`。
- Produces: `ContextPanel({ conv })`:展示客户头像/ID、会话状态、轮次、创建时间、当前接待模式。纯展示,无副作用(订单类信息本方案不拉,保持轻量;预留说明位)。

- [ ] **Step 1: 实现组件**

```tsx
// webui/src/components/workbench/ContextPanel.tsx
import { Card } from "@/components/ui/card";
import { avatarColor, initials, relativeTime, statusMeta, TONE_CLASS } from "./parts";
import type { WbConversation } from "@/lib/api";

export function ContextPanel({ conv }: { conv: WbConversation | null }) {
  if (!conv) return (
    <div className="hidden h-full items-center justify-center border-l bg-card/40 p-6 text-center text-xs text-muted-foreground lg:flex">
      选择左侧会话查看客户信息
    </div>
  );
  const sm = statusMeta(conv);
  return (
    <div className="hidden h-full flex-col gap-4 border-l bg-card/40 p-4 lg:flex">
      <div className="flex flex-col items-center gap-2 pt-2">
        <span className="flex h-14 w-14 items-center justify-center rounded-full text-lg font-medium text-white"
          style={{ background: avatarColor(conv.user_id) }}>
          {initials(conv.user_id)}
        </span>
        <span className="text-sm font-semibold">客户 {conv.user_id}</span>
        <span className={`rounded px-2 py-0.5 text-[11px] ${TONE_CLASS[sm.tone]}`}>{sm.label}</span>
      </div>
      <Card className="flex flex-col gap-2 p-3 text-xs">
        <Row k="会话 ID" v={conv.conversation_id} mono />
        <Row k="状态" v={conv.status === "open" ? "进行中" : "已结束"} />
        <Row k="对话轮次" v={String(conv.turns)} />
        <Row k="创建于" v={relativeTime(conv.created_at)} />
        <Row k="接待模式" v={conv.manual ? "人工" : "AI"} />
      </Card>
      <p className="text-[11px] leading-relaxed text-muted-foreground">
        提示:AI 会先接待并可查订单/物流/退款/议价;需要人工时在中栏「转人工接管」后回复,客户端即时可见。
      </p>
    </div>
  );
}

function Row({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-muted-foreground">{k}</span>
      <span className={`truncate ${mono ? "font-mono text-[11px]" : ""}`} title={v}>{v}</span>
    </div>
  );
}
```

- [ ] **Step 2: 类型/构建自检**

Run: `cd webui && npx tsc -b`
Expected: 无错误

- [ ] **Step 3: 提交**

```bash
git add webui/src/components/workbench/ContextPanel.tsx
git commit -m "feat(workbench): 客户上下文右栏"
```

---

## Task 10: 前端 — 组装工作台 `WorkbenchView.tsx` + 接入 App + 多客户 + 客户轮询

**Files:**
- Create: `webui/src/components/WorkbenchView.tsx`
- Modify: `webui/src/App.tsx`(`seat` → `WorkbenchView`;`?user=` 多客户)
- Modify: `webui/src/components/AppShell.tsx`(Tab 文案「坐席」→「工作台」)
- Modify: `webui/src/components/ChatView.tsx`(轮询接收人工回复)
- Modify: `app/api/app.py`(Task 4 的 1011 真实数据别名,见 Step 3)

**Interfaces:**
- Consumes: Task 5-9 的全部。
- Produces: `WorkbenchView()`:三栏布局 + 每 3s 轮询 `adminListConversations`;选中会话时轮询 `adminGetMessages`;`onToggleManual` 调 `adminTakeover` 后立即刷新;`onAfterReply` 直接用返回的 turns 更新中栏。

- [ ] **Step 1: 实现 WorkbenchView**

```tsx
// webui/src/components/WorkbenchView.tsx
import { useEffect, useRef, useState } from "react";
import { ConversationList } from "./workbench/ConversationList";
import { MessageThread } from "./workbench/MessageThread";
import { ContextPanel } from "./workbench/ContextPanel";
import {
  adminListConversations, adminGetMessages, adminTakeover,
  type WbConversation, type WbTurn,
} from "@/lib/api";

type Filter = "all" | "manual" | "open";

export function WorkbenchView() {
  const [convs, setConvs] = useState<WbConversation[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<string | undefined>();
  const [turns, setTurns] = useState<WbTurn[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const selRef = useRef<string | undefined>(undefined);
  selRef.current = selected;

  async function refreshList() {
    try { setErr(null); setConvs(await adminListConversations()); }
    catch (e: any) { setErr(String(e?.message || e)); }
  }
  async function refreshThread(sid: string) {
    try { setTurns(await adminGetMessages(sid)); } catch { /* 静默,下一轮重试 */ }
  }

  // 列表每 3s 刷新
  useEffect(() => {
    refreshList();
    const t = setInterval(refreshList, 3000);
    return () => clearInterval(t);
  }, []);

  // 选中会话:立即拉一次 + 每 3s 刷新消息(收到 AI/客户新消息)
  useEffect(() => {
    if (!selected) { setTurns([]); return; }
    refreshThread(selected);
    const t = setInterval(() => { if (selRef.current) refreshThread(selRef.current); }, 3000);
    return () => clearInterval(t);
  }, [selected]);

  const selConv = convs.find((c) => c.conversation_id === selected) || null;

  async function onToggleManual() {
    if (!selected) return;
    await adminTakeover(selected);
    await refreshList();
  }

  return (
    <div className="grid h-full grid-cols-[300px_1fr] lg:grid-cols-[300px_1fr_260px]">
      <ConversationList items={convs} selected={selected} onSelect={setSelected}
        filter={filter} onFilter={setFilter} />
      {selConv ? (
        <MessageThread sessionId={selConv.conversation_id} userId={selConv.user_id}
          manual={selConv.manual} turns={turns}
          onAfterReply={setTurns} onToggleManual={onToggleManual} />
      ) : (
        <div className="flex items-center justify-center text-sm text-muted-foreground">
          {err ? <span className="text-destructive">加载失败:{err}</span> : "从左侧选择一路会话开始接待"}
        </div>
      )}
      <ContextPanel conv={selConv} />
    </div>
  );
}
```

- [ ] **Step 2: 接入 App + Tab 文案**

`webui/src/App.tsx`:导入并把 `seat` 分支换成工作台:

```tsx
// 顶部 import 增加
import { WorkbenchView } from "@/components/WorkbenchView";
```

把:

```tsx
      {view === "seat" && <SeatView sessionId={sessionId} />}
```

改为:

```tsx
      {view === "seat" && <WorkbenchView />}
```

`webui/src/components/AppShell.tsx`:把导航里「坐席」文案改成「工作台」(定位到 `坐席` 字样一处替换为 `工作台`)。

- [ ] **Step 3: `?user=` 多客户 + 1011 真实数据别名**

`webui/src/App.tsx` 挂载 effect 里,demo 自动登录时优先用 URL 的 `?user=`:

把:

```tsx
        if (cfg.demo_mode && cfg.demo_user_id) {
          try {
            const r = await createUser(cfg.demo_user_id, "演示用户");
            setToken(r.token);
          } catch {
            try { const r = await login(cfg.demo_user_id); setToken(r.token); } catch { /* 回落登录门 */ }
          }
        }
```

改为:

```tsx
        const qsUser = new URLSearchParams(location.search).get("user");
        const demoId = qsUser || cfg.demo_user_id;   // ?user=xxx 以指定客户进入(多窗口演示并发)
        if (cfg.demo_mode && demoId) {
          try {
            const r = await createUser(demoId, "客户 " + demoId);
            setToken(r.token);
          } catch {
            try { const r = await login(demoId); setToken(r.token); } catch { /* 回落登录门 */ }
          }
        }
```

`app/api/app.py` 的 `/api/chat` demo 注入分支(Task 4 Step 3 的条件)扩展为支持 1011 别名(可选,想让 `?user=1011` 也有真实订单时):

```python
        _demo_tokens = {str(settings.demo_hmdp_user_id): settings.demo_hmdp_token, "1011": "demo-hmdp-token-1011"}
        if settings.demo_mode and not getattr(req, "hmdp_token", "") and str(req.user_id) in _demo_tokens:
            req.hmdp_token = _demo_tokens[str(req.user_id)]
```

- [ ] **Step 4: 客户侧轮询接收人工回复**

`webui/src/components/ChatView.tsx`:在现有"拉取历史"effect 之后新增一个轮询 effect(非流式时每 4s 对比历史气泡数,变多则重建 turns,以显示人工回复):

```tsx
  // 客户侧接收人工回复:非流式时轮询 history,发现新增气泡则重建(人工坐席消息会即时出现)
  useEffect(() => {
    let alive = true;
    const timer = setInterval(async () => {
      if (streaming) return;
      const bubbles = await getHistory(sessionId);
      if (!alive) return;
      setTurns((prev) => {
        const prevReplies = prev.filter((t) => t.reply).length;
        const asstCount = bubbles.filter((b) => b.role === "assistant").length;
        if (asstCount <= prevReplies) return prev;   // 无新增,保持(避免打断输入)
        const rebuilt: Turn[] = [];
        for (const b of bubbles) {
          if (b.role === "user") rebuilt.push({ id: ++idRef.current, userText: b.content, activity: [] });
          else if (rebuilt.length && !rebuilt[rebuilt.length - 1].reply) rebuilt[rebuilt.length - 1].reply = b.content;
          else rebuilt.push({ id: ++idRef.current, userText: "", activity: [], reply: b.content });
        }
        return rebuilt;
      });
    }, 4000);
    return () => { alive = false; clearInterval(timer); };
  }, [sessionId, streaming]);
```

> 说明:重建仅在 assistant 气泡数增加时触发,不打断用户正在输入/流式的当前轮。`userText:""` 的纯 assistant 轮即人工坐席主动消息。

- [ ] **Step 5: 类型/构建自检**

Run: `cd webui && npx tsc -b && npx vitest run`
Expected: 类型通过;既有前端测试(含 Task 5/6 新增)全绿

- [ ] **Step 6: 提交**

```bash
git add webui/src/components/WorkbenchView.tsx webui/src/App.tsx webui/src/components/AppShell.tsx webui/src/components/ChatView.tsx app/api/app.py
git commit -m "feat(workbench): 组装三栏工作台 + 多客户进入 + 客户侧接收人工回复"
```

---

## Task 11: 构建 + 端到端验证(多客户并发 + 人工接管)

**Files:**
- Modify: `web/dist/*`(构建产物)

**Interfaces:**
- Consumes: 全部前序任务。

- [ ] **Step 1: 构建前端**

Run: `cd webui && npm run build`
Expected: 输出到 `../web/dist`,无错误

- [ ] **Step 2: 重启 agent(载入新前端 + 新接口)**

Run: `powershell -File scripts\stop_all.ps1` 然后 `powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1`
Expected: 8010/8085/9123/6379/3306 全 UP

- [ ] **Step 3: 手动 E2E(浏览器)**

1. 开窗口 A:`http://127.0.0.1:8010/?user=1` → 以「客户 1」(小鱼同学,真实订单)进入,问「我的订单有哪些」,得到真实订单回复。
2. 开窗口 B(无痕):`http://127.0.0.1:8010/?user=alice` → 以「客户 alice」进入,问「你们几点上班」。
3. 任一窗口点顶部「工作台」Tab:**左栏应同时看到 客户1 与 alice 两路会话**,各带预览与「AI 接待」徽标。
4. 选中 alice 会话 → 中栏看到其消息;点「转人工接管」→ 徽标变「人工中」,输入「您好,人工小助手为您服务」发送。
5. 回到窗口 B(alice):**≤4s 内应出现这条人工回复气泡**。
6. alice 再发消息 → 该会话不再由 AI 回复(人工模式短路)。

Expected:以上 6 步全部成立;后端日志可见 `/api/admin/conversations`、`/api/admin/session/*/reply`、以及窗口 B 的 `/api/session/*/history` 轮询。

- [ ] **Step 4: 提交构建产物**

```bash
git add web/dist
git commit -m "build(workbench): 重建前端产物"
```

- [ ] **Step 5: 推送**

```bash
git push origin feature/w1-service-streaming
```

---

## Self-Review

**1. Spec coverage(需求→任务映射):**
- 多客户并发可见 → Task 1(列所有会话)+ Task 10 Step 3(`?user=` 多客户)+ Task 11 Step 3(双窗口验证)✓
- 坐席读任意会话 → Task 2 ✓
- 人工接管 + 人工回复 → Task 3(注入)+ Task 8(回复框)+ Task 10(接管开关)✓
- 客户侧即时收到人工回复 → Task 10 Step 4(轮询)✓
- 前端更好看 → Task 6-9(头像/状态色/相对时间/三栏布局/空态)✓
- 不塌缩多身份 → Task 4 ✓

**2. Placeholder 扫描:** 各步均含真实代码/命令/预期;无 TODO/TBD。Task 4 Step 4 的 1011 播种标注为"可选",Task 10 Step 3 给了对应真实数据别名,二者一致。

**3. 类型一致性:** `WbConversation`/`WbTurn` 在 Task 5 定义,Task 6-10 一致引用;`adminReply` 返回 `WbTurn[]` 与 Task 3 后端 `{turns:[...]}` 对齐;`statusMeta` 的 tone 三值与 `TONE_CLASS` 键一致;`reconstruct_bubbles` 的输出 `{role,content}` 与前端 `WbTurn` 一致。

**4. 风险点(实现时留意):**
- `MessageBubble` 的 props 若非 `{role, children}`,Task 8/9 需按其实际签名调整(实现前先看该组件)。
- shadcn 若无 `Textarea`,已用原生 `<textarea>`,无需新增。
- `session_snapshot_enabled`/`get_session_snapshot` 若命名不同,Task 2 按 `app/api/app.py` 现有 `session_history` 的写法对齐(该文件已在用同名字段)。
