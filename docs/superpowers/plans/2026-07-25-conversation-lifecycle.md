# 会话生命周期生产化(服务端签发 + 状态机 + 翻篇) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 会话 ID 改为**服务端签发**的 `conversation_id`(uuid)并带 **open/closed 状态机**——空闲超时/手动结束即关闭翻篇,下次来是新会话;前端只持有服务端发的 ID;新增"历史会话列表";对齐真实客服"服务单"语义(一段对话 = 一个会话 = 运营指标计量单位)。

**Architecture:** 三层:①数据层——业务库 `conversations` 表(id/user/status/时间/关闭原因)+ CRUD;②服务层——`open_or_reuse`(同用户复用未关闭会话,多端一致)与 `ensure_active`(向已关闭/未知 ID 发消息 → 服务端自动开新会话,**绝不采纳客户端自造 ID**),接入 `/api/chat`(SSE 首帧 `conversation` 事件告知前端当前生效 ID)、巩固/reset/reaper 三处关闭点;③前端——挂载/切用户时调 open,收 `conversation` 事件即更新,结束按钮翻篇清屏,历史会话面板只读回看。

**Tech Stack:** Python 3.11 标准库(uuid/sqlite3)+ 现有 FastAPI/SessionManager/React(webui)。无新依赖。

## Global Constraints

- **服务端签发铁律**:`conversation_id = "c-" + uuid4().hex[:16]` 只由服务端生成;客户端传来的未知 ID **不得**被采纳为新会话(防伪造),一律由 `ensure_active` 换发新 ID。
- **向后兼容**:旧格式 session_id(`default--xxx`)的历史回显(GET history)照常可读;向旧 ID 发消息按"未知 ID"处理 → 自动开新会话(老会话数据不迁移、不删除)。
- **长期记忆不受影响**:记忆按 `user_id` 存,会话翻篇后记忆延续(不改 memory 任何代码)。
- **事件协议只增不改**:新增 SSE 事件 `{"type":"conversation","conversation_id":...,"status":"active"|"rotated"}` 作为 `/api/chat` 首帧;其余事件不动。
- **user_id 仍为自报**(演示项目边界):在 README/文档标注"生产需替换为登录态(JWT)签发";本改造不做认证。
- 表放业务库 `ecom.db`(与 session_archive 同库同风格:`app/db/database.py`)。
- 关闭原因枚举:`idle`(reaper 空闲)/ `manual`(结束会话按钮)/ `reset`(重置对话)。关闭幂等(重复关不报错不改时间)。
- ChatRequest 字段名 `session_id` **保持不变**(承载 conversation_id,全链路下游——SessionManager/锁/存储/Langfuse——零改动)。
- 每任务:独立提交 + 指定测试文件绿(**切勿全量 pytest**,含真网络用例超时;逐文件跑)+ commit 末行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;推送 `origin feature/w1-service-streaming`,**绝不 push upstream**。
- `.venv/Scripts/python.exe`;测试不 print emoji(Windows gbk)。

## File Structure

| 文件 | 任务 | 责任 |
|---|---|---|
| `app/db/database.py`(改) | T1 | `conversations` 表 + create/get/close/latest_open/list_by_user |
| `app/api/conversations.py`(新) | T2 | `open_or_reuse(db, user_id)` / `ensure_active(db, session_id, user_id)` 纯逻辑 |
| `app/api/app.py`(改) | T2/T3 | `POST /api/conversation/open`、`GET /api/conversations`;`/api/chat` 接 ensure_active + 首帧事件;consolidate/reset 关闭点 |
| `app/api/session_manager.py`(改) | T3 | reaper 回收时 `close_conversation(reason="idle")` |
| `webui/src/lib/api.ts` / `App.tsx` / `ChatView.tsx`(改) | T4 | 服务端 ID 生命周期 + conversation 事件 + 历史会话面板 |
| `tests/test_conversations.py`(新)、`tests/test_api.py`(增) | T1-T3 | 各层测试 |

---

### Task T1: conversations 表 + CRUD(数据层)

**Files:**
- Modify: `app/db/database.py`(init_schema 的 executescript 内加表;类尾加方法,风格对齐 `archive_session`/`get_archived_session`——短连接 `self.connect()` + try/finally close)
- Test: `tests/test_conversations.py`(新)

**Interfaces:**
- Produces(T2/T3 依赖,签名逐字):
  - `create_conversation(self, user_id: str) -> dict`(生成 `"c-"+uuid4().hex[:16]`,status="open",created_at=now iso;返回 `{"conversation_id","user_id","status","created_at"}`)
  - `get_conversation(self, conversation_id: str) -> Optional[dict]`(含 closed_at/close_reason)
  - `close_conversation(self, conversation_id: str, reason: str) -> bool`(open→closed 置 closed_at/close_reason 返回 True;已关/不存在返回 False 且**不改动**——幂等)
  - `latest_open_conversation(self, user_id: str) -> Optional[dict]`(该用户最近一条 open,按 created_at DESC)
  - `list_conversations(self, user_id: str, limit: int = 20) -> list[dict]`(该用户全部,created_at DESC)

- [ ] **Step 1: 写失败测试** `tests/test_conversations.py`

```python
"""会话生命周期数据层:服务端签发 + open/closed 状态机。临时库,全离线。"""

from app.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_schema()
    return db


def test_create_issues_server_id_and_open(tmp_path):
    db = _db(tmp_path)
    c = db.create_conversation("u1")
    assert c["conversation_id"].startswith("c-") and len(c["conversation_id"]) == 18
    assert c["status"] == "open" and c["user_id"] == "u1"
    got = db.get_conversation(c["conversation_id"])
    assert got["status"] == "open" and got["close_reason"] is None


def test_close_sets_reason_and_is_idempotent(tmp_path):
    db = _db(tmp_path)
    cid = db.create_conversation("u1")["conversation_id"]
    assert db.close_conversation(cid, "manual") is True
    got = db.get_conversation(cid)
    assert got["status"] == "closed" and got["close_reason"] == "manual"
    first_closed_at = got["closed_at"]
    assert db.close_conversation(cid, "idle") is False        # 已关:幂等,不覆盖
    got2 = db.get_conversation(cid)
    assert got2["close_reason"] == "manual" and got2["closed_at"] == first_closed_at
    assert db.close_conversation("c-notexist12345678", "idle") is False


def test_latest_open_per_user(tmp_path):
    db = _db(tmp_path)
    a = db.create_conversation("u1")["conversation_id"]
    db.close_conversation(a, "manual")
    b = db.create_conversation("u1")["conversation_id"]
    db.create_conversation("u2")
    assert db.latest_open_conversation("u1")["conversation_id"] == b
    db.close_conversation(b, "idle")
    assert db.latest_open_conversation("u1") is None


def test_list_conversations_desc_and_limit(tmp_path):
    db = _db(tmp_path)
    ids = [db.create_conversation("u1")["conversation_id"] for _ in range(3)]
    rows = db.list_conversations("u1", limit=2)
    assert len(rows) == 2
    assert rows[0]["conversation_id"] == ids[-1]      # 最新在前
    assert db.list_conversations("u_none") == []
```

- [ ] **Step 2: 跑失败** → `.venv/Scripts/python.exe -m pytest tests/test_conversations.py -q` FAIL(方法不存在)

- [ ] **Step 3: 实现**——init_schema 加表:

```sql
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    closed_at TEXT,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, status);
```

方法(类尾,签名见 Interfaces;逐字实现要点):

```python
def create_conversation(self, user_id: str) -> dict:
    import uuid
    from datetime import datetime
    cid = "c-" + uuid.uuid4().hex[:16]
    now = datetime.now().isoformat(timespec="seconds")
    conn = self.connect()
    try:
        conn.execute(
            "INSERT INTO conversations (conversation_id, user_id, status, created_at) "
            "VALUES (?, ?, 'open', ?)", (cid, user_id, now))
        conn.commit()
    finally:
        conn.close()
    return {"conversation_id": cid, "user_id": user_id, "status": "open", "created_at": now}

def get_conversation(self, conversation_id: str):
    conn = self.connect()
    try:
        row = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?",
                           (conversation_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def close_conversation(self, conversation_id: str, reason: str) -> bool:
    from datetime import datetime
    conn = self.connect()
    try:
        cur = conn.execute(
            "UPDATE conversations SET status='closed', closed_at=?, close_reason=? "
            "WHERE conversation_id = ? AND status='open'",
            (datetime.now().isoformat(timespec="seconds"), reason, conversation_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def latest_open_conversation(self, user_id: str):
    conn = self.connect()
    try:
        row = conn.execute(
            "SELECT * FROM conversations WHERE user_id=? AND status='open' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def list_conversations(self, user_id: str, limit: int = 20) -> list:
    conn = self.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE user_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?", (user_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
```

- [ ] **Step 4: 跑通过** → `pytest tests/test_conversations.py tests/test_db_schema.py tests/test_db_repository.py -q` PASS

- [ ] **Step 5: 提交** `feat(db): T1 conversations 表——服务端签发会话 + open/closed 状态机`

---

### Task T2: 会话服务层 + open/list 端点

**Files:**
- Create: `app/api/conversations.py`
- Modify: `app/api/app.py`(create_app 内加两个端点,放在 `/api/session/reset` 附近)
- Test: `tests/test_conversations.py`(增)、`tests/test_api.py`(增)

**Interfaces:**
- Consumes: T1 的五个 db 方法。
- Produces(T3/T4 依赖):
  - `open_or_reuse(db, user_id: str) -> dict`:有 open 会话 → 原样返回(多端/刷新一致);没有 → `create_conversation`。
  - `ensure_active(db, session_id: str, user_id: str) -> tuple[str, bool]`:返回 `(生效的 conversation_id, rotated)`。规则:ID 存在且 open → `(原ID, False)`;存在但 closed → 开新 `(新ID, True)`;**不存在(含旧格式/伪造)→ 开新 `(新ID, True)`**。
  - `POST /api/conversation/open`,body `{"user_id": "..."}` → `open_or_reuse` 结果(200)。
  - `GET /api/conversations?user_id=&limit=` → `{"conversations": [...]}`(list_conversations)。

- [ ] **Step 1: 写失败测试**(服务层加到 `tests/test_conversations.py`;端点加到 `tests/test_api.py` 用现有 `_client()` 手法)

```python
# --- tests/test_conversations.py 增 ---
from app.api.conversations import open_or_reuse, ensure_active


def test_open_or_reuse_returns_existing_open(tmp_path):
    db = _db(tmp_path)
    first = open_or_reuse(db, "u1")
    again = open_or_reuse(db, "u1")
    assert again["conversation_id"] == first["conversation_id"]   # 复用,不重复开
    db.close_conversation(first["conversation_id"], "manual")
    third = open_or_reuse(db, "u1")
    assert third["conversation_id"] != first["conversation_id"]   # 关了才翻篇


def test_ensure_active_open_passthrough(tmp_path):
    db = _db(tmp_path)
    cid = db.create_conversation("u1")["conversation_id"]
    assert ensure_active(db, cid, "u1") == (cid, False)


def test_ensure_active_closed_rotates(tmp_path):
    db = _db(tmp_path)
    cid = db.create_conversation("u1")["conversation_id"]
    db.close_conversation(cid, "idle")
    new_id, rotated = ensure_active(db, cid, "u1")
    assert rotated is True and new_id != cid and new_id.startswith("c-")


def test_ensure_active_never_adopts_client_id(tmp_path):
    """安全铁律:客户端自造/旧格式 ID 不被采纳,服务端换发。"""
    db = _db(tmp_path)
    new_id, rotated = ensure_active(db, "default--acbuu9p4", "u1")
    assert rotated is True and new_id.startswith("c-")
    assert db.get_conversation("default--acbuu9p4") is None      # 未被写库


# --- tests/test_api.py 增(用现有 _client/_parse_sse) ---
def test_conversation_open_and_list():
    client, _ = _client()
    r1 = client.post("/api/conversation/open", json={"user_id": "u9"})
    assert r1.status_code == 200
    cid = r1.json()["conversation_id"]
    assert cid.startswith("c-")
    r2 = client.post("/api/conversation/open", json={"user_id": "u9"})
    assert r2.json()["conversation_id"] == cid                   # 复用
    r3 = client.get("/api/conversations", params={"user_id": "u9"})
    assert any(c["conversation_id"] == cid for c in r3.json()["conversations"])
```

注意:`_client()` 的 app 用真业务库 `get_db()`(settings.db_path 默认路径)——测试须隔离:在这两个 API 测试里 `monkeypatch.setattr(settings, "db_path", str(tmp_path/"t.db"))` 并重新 init(看 `app/db/__init__.py` 的 get_db 是否缓存单例——**若是单例,加 `set_db(None)`/reset 手段或 monkeypatch get_db 返回临时 Database**;以实际代码为准,报告里说明选择)。

- [ ] **Step 2: 跑失败** → 两文件 FAIL(模块/端点不存在)

- [ ] **Step 3: 实现** `app/api/conversations.py`

```python
"""会话生命周期服务层(生产语义:服务端签发,一段对话一个会话)。

铁律:conversation_id 只由服务端生成;客户端传来的未知 ID(旧格式/伪造)
一律不采纳,由 ensure_active 换发新会话——防会话伪造,也兼容旧数据平滑过渡。
"""

from __future__ import annotations


def open_or_reuse(db, user_id: str) -> dict:
    """取该用户最近的 open 会话(多端/刷新一致);没有则服务端新开一个。"""
    existing = db.latest_open_conversation(user_id)
    if existing is not None:
        return existing
    return db.create_conversation(user_id)


def ensure_active(db, session_id: str, user_id: str) -> tuple[str, bool]:
    """确保拿到一个可用(open)的会话 ID;返回 (生效ID, 是否翻篇/换发)。"""
    conv = db.get_conversation(session_id)
    if conv is not None and conv["status"] == "open":
        return session_id, False
    return db.create_conversation(user_id)["conversation_id"], True
```

app.py 端点(import `from app.api.conversations import open_or_reuse`;`from app.db import get_db`;body 模型加到 `app/api/schemas.py`:`class OpenConversationRequest(BaseModel): user_id: str = "default"`):

```python
@app.post("/api/conversation/open")
def conversation_open(req: OpenConversationRequest):
    """服务端签发/复用会话:同用户已有 open 会话则复用(多端一致),否则新开。"""
    return open_or_reuse(get_db(), req.user_id)

@app.get("/api/conversations")
def conversations_list(user_id: str = "default", limit: int = 20):
    return {"conversations": get_db().list_conversations(user_id, limit=limit)}
```

- [ ] **Step 4: 跑通过** → `pytest tests/test_conversations.py tests/test_api.py -q` PASS
- [ ] **Step 5: 提交** `feat(api): T2 会话服务层——open_or_reuse/ensure_active + 开会话/列表端点`

---

### Task T3: 接入 /api/chat 翻篇 + 三处关闭点

**Files:**
- Modify: `app/api/app.py`(chat 端点开头 + consolidate + reset)
- Modify: `app/api/session_manager.py`(`_consolidate_and_evict`)
- Test: `tests/test_api.py`(增)、`tests/test_session_reaper.py`(增)

**Interfaces:**
- Consumes: T2 的 `ensure_active`;T1 的 `close_conversation`。
- Produces(T4 依赖):SSE 首帧 `{"type":"conversation","conversation_id":"c-...","status":"active"|"rotated"}`;`POST /api/session/reset` 响应变为 `{"status":"reset","conversation_id":"c-新ID"}`(close(reset)+开新);consolidate 响应增 `"conversation_closed": true`。

- [ ] **Step 1: 写失败测试**(加到 `tests/test_api.py`;沿用 monkeypatch 临时 db 的手法)

```python
def test_chat_emits_conversation_event_and_rotates_closed():
    client, _ = _client()   # + 临时 db monkeypatch(同 T2)
    cid = client.post("/api/conversation/open", json={"user_id": "u9"}).json()["conversation_id"]
    # open 会话:首帧 conversation,status=active,ID 不变
    r = client.post("/api/chat", json={"session_id": cid, "message": "你好", "user_id": "u9"})
    events = _parse_sse(r.text)
    conv = next(e for e in events if e["type"] == "conversation")
    assert conv["conversation_id"] == cid and conv["status"] == "active"
    # 关闭后再发:换发新 ID,status=rotated,且 agent 用的是新 ID(fake factory 记录 session_path)
    from app.db import get_db
    get_db().close_conversation(cid, "manual")
    r2 = client.post("/api/chat", json={"session_id": cid, "message": "在吗", "user_id": "u9"})
    conv2 = next(e for e in _parse_sse(r2.text) if e["type"] == "conversation")
    assert conv2["status"] == "rotated" and conv2["conversation_id"] != cid


def test_reset_rotates_conversation():
    client, _ = _client()
    cid = client.post("/api/conversation/open", json={"user_id": "u9"}).json()["conversation_id"]
    r = client.post("/api/session/reset", json={"session_id": cid})
    body = r.json()
    assert body["conversation_id"] != cid
    from app.db import get_db
    assert get_db().get_conversation(cid)["close_reason"] == "reset"


# --- tests/test_session_reaper.py 增 ---
def test_reaper_closes_conversation(tmp_path, monkeypatch):
    """空闲回收时会话置 closed(reason=idle)。"""
    # 用现有 reaper 测试的注入手法(clock/factory);断言:
    # sweep 后 get_db().get_conversation(cid)["close_reason"] == "idle"
```

(reaper 测试的完整代码由实现者按 `tests/test_session_reaper.py` 现有夹具风格补齐——该文件已有 clock 注入与 fake agent 先例,断言点如上两行,必须真实存在且通过。)

- [ ] **Step 2: 跑失败**

- [ ] **Step 3: 实现** —— app.py chat 端点开头(限流之前)插入:

```python
# 会话生命周期:确保 ID 可用;closed/未知(旧格式/伪造)→ 服务端换发翻篇
from app.api.conversations import ensure_active
active_id, rotated = ensure_active(get_db(), req.session_id, req.user_id)
req.session_id = active_id   # 下游(锁/agent/存储/观测)全部用生效 ID
```

`event_stream()` 开头(锁之前)先发首帧:

```python
yield _sse_frame({"type": "conversation", "conversation_id": active_id,
                  "status": "rotated" if rotated else "active"})
```

注意:**fast-path 与人工接管短路分支也要带上首帧**——把 `_reply_stream(text)` 调用处改为 `_reply_stream(text, conversation=(active_id, rotated))`,`_reply_stream` 增可选参数,在 reply 帧前多 yield 一个 conversation 帧(None 时行为不变,兼容其它调用点)。

consolidate 端点(现有 `background_trace` 块之后):

```python
get_db().close_conversation(session_id, "manual")   # 结束会话:翻篇
# 返回体增: "conversation_closed": True
```

reset 端点:

```python
@app.post("/api/session/reset", dependencies=[Depends(admin_auth)])
def reset(req: ResetRequest):
    manager.reset(req.session_id)
    from app.api.conversations import open_or_reuse
    get_db().close_conversation(req.session_id, "reset")
    # 老会话已关;立刻给前端一个新会话,免得下一条消息再走 rotated 换发
    new_conv = open_or_reuse(get_db(), getattr(req, "user_id", "default"))
    return {"status": "reset", "conversation_id": new_conv["conversation_id"]}
```

(`ResetRequest` 增字段 `user_id: str = "default"`。)

session_manager `_consolidate_and_evict`(archive 之后):

```python
try:
    from app.db import get_db
    get_db().close_conversation(session_id, "idle")   # 生命周期:空闲即翻篇
except Exception:
    pass
```

- [ ] **Step 4: 跑通过** → `pytest tests/test_api.py tests/test_conversations.py tests/test_session_reaper.py tests/test_consolidate_api.py tests/test_session_manager.py -q` PASS
- [ ] **Step 5: 提交** `feat(api): T3 /api/chat 翻篇换发 + 巩固/reset/reaper 三处关闭点`

---

### Task T4: 前端——服务端 ID 生命周期 + 历史会话面板

**Files:**
- Modify: `webui/src/lib/api.ts`(getSessionId/getBaseToken 删除,换 openConversation/listConversations)
- Modify: `webui/src/App.tsx`(会话 ID 变 state,挂载/切用户时 open)
- Modify: `webui/src/components/ChatView.tsx`(conversation 事件、结束按钮翻篇清屏、历史会话面板)
- Test: `webui/src/tests/`(现有 4 个测试文件保持绿;新增断言见 Step 3)
- 构建:`cd webui && npx vitest run && npm run build`(产 web/dist 提交)

**Interfaces:**
- Consumes: T2 端点、T3 的 conversation SSE 事件与 reset 新响应。
- Produces: 用户可见行为——刷新/切用户回到同一 open 会话;点"结束会话·巩固记忆"→ 翻篇清屏新会话;收到 `rotated` 首帧 → 无感切到新 ID;"历史会话"面板列出已关会话,点击只读回看。

- [ ] **Step 1: api.ts 改造**

```ts
// 删除 getBaseToken/getSessionId(服务端签发后前端不再自造 ID)
export async function openConversation(userId: string): Promise<{ conversation_id: string }> {
  const r = await fetch("/api/conversation/open", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId }),
  });
  return r.json();
}
export type ConversationMeta = { conversation_id: string; status: string; created_at: string; close_reason?: string | null };
export async function listConversations(userId: string): Promise<ConversationMeta[]> {
  const r = await fetch(`/api/conversations?user_id=${encodeURIComponent(userId)}`);
  return (await r.json()).conversations;
}
```

- [ ] **Step 2: App.tsx**——`sessionId` 从同步计算改为 state + effect:

```tsx
const [sessionId, setSessionId] = useState<string>("");
useEffect(() => {
  let alive = true;
  openConversation(userId).then((c) => { if (alive) setSessionId(c.conversation_id); });
  return () => { alive = false; };
}, [userId]);
if (!sessionId) return <div className="p-8 text-sm text-muted-foreground">正在建立会话…</div>;
```

reset 按钮处理:用响应里的新 `conversation_id` 直接 `setSessionId`(不再手动重开)。把 `setSessionId` 作为 `onConversation` prop 传给 ChatView。

- [ ] **Step 3: ChatView.tsx**——三处:
  1. `useChatStream` 的 `onEvent` 加分支:`e.type === "conversation" && e.status === "rotated"` → 调 `props.onConversation(e.conversation_id)`(下一条消息用新 ID;当前流照常收完)。
  2. "结束会话·巩固记忆" onConsolidate 成功后:`openConversation(userId)` → `props.onConversation(新ID)` + 清空 turns(翻篇清屏,长期记忆面板照常展示巩固结果)。
  3. 历史会话面板:头部加"历史会话"按钮 → 展开列表(`listConversations(userId)`,显示 created_at/status/close_reason)→ 点击某条 `getHistory(conversation_id)` 在面板内只读渲染消息气泡(复用现有历史渲染逻辑;不改变当前活动会话)。
  现有 `restored` 历史回显逻辑保持(fetch 当前 sessionId 的 history)。

- [ ] **Step 4: 测试 + 构建**

Run: `cd webui && npx vitest run && npm run build`
Expected: 现有 4 文件全 PASS(若组件测试因 props 变化需同步——`ChatView` 新增必传 prop `onConversation`,测试里传 `() => {}`);build 产出 web/dist。

- [ ] **Step 5: 提交** `feat(webui): T4 服务端会话生命周期——翻篇/无感换发/历史会话面板`(含 web/dist)

---

### Task T5: 端到端冒烟(控制方执行,不派实现者)

- [ ] 起服务(`.venv` uvicorn)→ 前端加载出现"正在建立会话…"后进入,会话 ID 形如 `c-xxxx`(看头部"会话"字样)
- [ ] 聊一句(带偏好,如"我喜欢红色运动鞋")→ 正常回复;刷新页面 → 历史还在(open 会话复用)
- [ ] 点"结束会话·巩固记忆"→ 长期记忆面板出现事实;聊天区清空;头部会话 ID 变化(翻篇)
- [ ] 再聊一句"我喜欢什么颜色?"→ 新会话里能答"红色"(长期记忆跨会话延续 ✅)
- [ ] "历史会话"面板:能看到上一条 closed(manual)会话,点击可回看消息
- [ ] 向旧格式 ID 发消息(curl `session_id="default--test"`)→ 首帧 `conversation status=rotated`,服务端换发
- [ ] Langfuse Sessions 视图:新老两段对话是**两个不同 Session** ✅(生产语义达成)
- [ ] 更新 `docs/Harness对齐-H1H3-工作总结与效果.md` 无需改;在 `.superpowers/sdd/progress.md` 记账

## 总量与顺序

T1(数据层,~0.3d)→ T2(服务层+端点,~0.3d)→ T3(chat 接入+关闭点,~0.5d)→ T4(前端,~0.5d)→ T5(冒烟,~0.2d),共 **~1.8 人日**。严格顺序。

## Self-Review

- **覆盖核对**:服务端签发(T1/T2)✅;状态机+幂等关闭(T1)✅;三处关闭点 idle/manual/reset(T3)✅;不采纳客户端 ID(T2 ensure_active + 专项测试)✅;前端只持服务端 ID + 无感换发 + 翻篇清屏 + 历史面板(T4)✅;记忆跨会话延续(T5 冒烟第 4 条验证,零代码改动)✅;旧 ID 兼容(history 端点不动 + ensure_active 换发,T3/T5)✅;Langfuse Sessions 语义(T5)✅。
- **占位符扫描**:reaper 测试给了断言点与夹具指引(该文件既有先例),其余全部完整代码;无 TBD。
- **类型一致性**:`ensure_active -> tuple[str, bool]` 在 T2 定义/T3 消费一致;`conversation` 事件字段(conversation_id/status)T3 产/T4 消费一致;`open_or_reuse` 返回 dict 含 conversation_id,T2 端点/T3 reset/T4 前端一致;db 五方法签名 T1↔T2/T3 一致。
- **已知取舍**:一用户同时最多一个 open 会话(open_or_reuse 复用语义)——符合"单渠道客服"现实;多渠道并行会话不在本期。fast-path/人工接管短路分支的首帧覆盖已在 T3 明确。
