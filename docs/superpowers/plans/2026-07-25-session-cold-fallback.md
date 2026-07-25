# 会话冷副本 + 历史回显兜底(热会话过期不再丢历史) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development(推荐)或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 修复"redis 热会话 TTL 过期 + 服务重启 reaper 失忆 → 会话历史彻底丢失"的存储衔接缺陷:**(B)** 每回合保存时同步写一份**会话冷快照**(SQLite upsert,永久,不依赖 reaper/内存态);**(A)** 历史回显在热存储 miss 时**兜底读冷快照**——组合成"热会话快、冷副本全、历史永不空"。

**Architecture:** 新增独立表 `session_snapshots(session_id PK, ...)` 走 **upsert**(每会话只留最新态),与审计用的 append 型 `session_archive` **职责分离、互不干扰、零迁移**。写路径:`EcomAgent` 每次 `store.save()` 之后 best-effort upsert 快照(session_id 从 `session_path` 的 stem 取,与 RedisSessionStore 的 key 语义一致)。读路径:`/api/session/{id}/history` 的 `peek_messages` 为空时 fallback 读快照;U3 的归属校验在 fallback 前已生效(conversations 表持久,不随 redis 过期)。全程 `session_snapshot_enabled` 门控,best-effort 异常不影响主流程。

**Tech Stack:** 现有 SQLite(ecom.db)、SessionManager/EcomAgent、标准库。零新依赖。

## Global Constraints

- **新增表,不碰 `session_archive`**:`session_snapshots(session_id TEXT PRIMARY KEY, user_id TEXT, messages TEXT, summary TEXT, updated_at TEXT)`;加进 `init_schema` 的 executescript(`CREATE TABLE IF NOT EXISTS`,新库/旧库都安全,无 ALTER 无迁移)。
- **upsert 语义(逐字)**:`upsert_session_snapshot(session_id, user_id, messages, summary)` 用 `INSERT ... ON CONFLICT(session_id) DO UPDATE`——每会话恒一行最新态;`get_session_snapshot(session_id) -> dict | None`(含 messages 已 `json.loads` 成 list;坏 JSON → messages=[])。
- **session_id 取值铁律**:写快照时 `session_id = Path(self.session_path).stem`(与 `RedisSessionStore` 用 `Path(key).stem` 做 key 完全一致,保证热/冷同名);stem 为空/`session` 默认名时**跳过**(裸 agent/CLI 无独立会话,不污染)。
- **best-effort 铁律**:快照写入与读取任何异常一律吞掉(`try/except`),绝不影响对话主流程或历史接口 500。
- **门控**:`session_snapshot_enabled: bool = True`(关=不写不读,回退现状);`tests/conftest.py` **不强制关**(快照走 SQLite 本地、无网络、无副作用外溢,既有测试用真 ecom.db 的本就少;但快照写入需 session_path 有 stem——裸 agent 的 `x.json` stem=`x` 会写,测试用临时 db 隔离即可)。**例外**:若某既有测试断言 `session_archive` 或 db 行数,快照表独立不影响;若 EcomAgent 单测无 db 环境导致 get_db() 建表——get_db() 已幂等 init_schema,安全。
- **归属校验优先于 fallback**:history 端点 auth_enabled 时先做 U3 的归属校验(读 `get_conversation`,持久不过期),**通过后**才 fallback 读快照;快照自身也带 user_id 作二次防线(快照 user_id != token 用户 → 不返回)。
- **A 读取顺序**:热存储(`peek_messages`)有数据 → 用它;为空 → fallback 快照;都无 → 空列表(现状)。
- 每任务:独立提交 + 指定测试文件绿(**切勿全量 pytest**);commit 末行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;推送 origin,绝不 upstream;`.venv/Scripts/python.exe`;测试不 print emoji。

## File Structure

| 文件 | 任务 | 责任 |
|---|---|---|
| `app/db/database.py`(改) | S1 | `session_snapshots` 表 + `upsert_session_snapshot`/`get_session_snapshot` |
| `app/config/settings.py`(改) | S1 | `session_snapshot_enabled` |
| `app/agent/chat.py`(改) | S2(B) | 每次 `save`/回合结束后 best-effort 写快照 |
| `app/api/app.py`(改 history 端点) | S3(A) | 热 miss → fallback 快照 + 归属二次校验 |
| `tests/test_session_snapshot.py`(新)、`tests/test_api.py`(增) | S1-S3 | 各层测试 |

---

### Task S1: session_snapshots 表 + upsert/get 方法

**Files:**
- Modify: `app/db/database.py`(表进 init_schema;方法进类尾,短连接风格同 `get_conversation`)、`app/config/settings.py`
- Test: `tests/test_session_snapshot.py`(新)

**Interfaces:**
- Produces(S2/S3 依赖,签名逐字):
  - `upsert_session_snapshot(self, session_id: str, user_id: str, messages: list, summary: str | None) -> None`
  - `get_session_snapshot(self, session_id: str) -> Optional[dict]`(返回 `{"session_id","user_id","messages": list,"summary","updated_at"}`;坏 JSON → messages=[])
  - `settings.session_snapshot_enabled: bool`

- [ ] **Step 1: 写失败测试** `tests/test_session_snapshot.py`

```python
"""会话冷快照:upsert 语义 + 读取。临时库,全离线。"""

from app.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    return db


def test_upsert_creates_then_overwrites(tmp_path):
    db = _db(tmp_path)
    db.upsert_session_snapshot("c-1", "u1", [{"role": "user", "content": "第一次"}], None)
    db.upsert_session_snapshot("c-1", "u1",
        [{"role": "user", "content": "第一次"}, {"role": "assistant", "content": "回复"}], "摘要")
    snap = db.get_session_snapshot("c-1")
    assert snap["user_id"] == "u1"
    assert len(snap["messages"]) == 2 and snap["summary"] == "摘要"    # 覆盖为最新,非追加
    # 只留一行(upsert 不堆积)
    import sqlite3
    conn = sqlite3.connect(db.db_path)
    n = conn.execute("SELECT COUNT(*) FROM session_snapshots WHERE session_id='c-1'").fetchone()[0]
    conn.close()
    assert n == 1


def test_get_missing_returns_none(tmp_path):
    assert _db(tmp_path).get_session_snapshot("c-none") is None


def test_messages_roundtrip_as_list(tmp_path):
    db = _db(tmp_path)
    msgs = [{"role": "user", "content": "查订单"}, {"role": "tool", "content": '{"ok":true}'}]
    db.upsert_session_snapshot("c-2", "u1", msgs, None)
    got = db.get_session_snapshot("c-2")
    assert isinstance(got["messages"], list) and got["messages"][1]["role"] == "tool"


def test_snapshot_table_independent_of_archive(tmp_path):
    """快照与审计归档互不干扰。"""
    db = _db(tmp_path)
    db.upsert_session_snapshot("c-3", "u1", [{"role": "user", "content": "x"}], None)
    db.archive_session("c-3", "u1", [{"role": "user", "content": "x"}], None)
    assert db.get_session_snapshot("c-3") is not None
    assert db.get_archived_session("c-3") is not None      # 两张表各存各的
```

- [ ] **Step 2: 跑失败** → `.venv/Scripts/python.exe -m pytest tests/test_session_snapshot.py -q` FAIL

- [ ] **Step 3: 实现**——init_schema 的 executescript 内(紧随 `conversations` 表)加:

```sql
CREATE TABLE IF NOT EXISTS session_snapshots (
    session_id TEXT PRIMARY KEY,
    user_id TEXT,
    messages TEXT,
    summary TEXT,
    updated_at TEXT
);
```

类尾方法:

```python
def upsert_session_snapshot(self, session_id: str, user_id: str,
                            messages: list, summary) -> None:
    """会话冷快照:每会话恒一行最新态(upsert)。热会话过期后供历史回显兜底。"""
    conn = self.connect()
    try:
        conn.execute(
            "INSERT INTO session_snapshots (session_id, user_id, messages, summary, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "user_id=excluded.user_id, messages=excluded.messages, "
            "summary=excluded.summary, updated_at=excluded.updated_at",
            (session_id, user_id, json.dumps(messages or [], ensure_ascii=False),
             summary, self._now()),
        )
        conn.commit()
    finally:
        conn.close()

def get_session_snapshot(self, session_id: str):
    conn = self.connect()
    try:
        row = conn.execute(
            "SELECT session_id, user_id, messages, summary, updated_at "
            "FROM session_snapshots WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["messages"] = json.loads(d["messages"]) if d["messages"] else []
        except (ValueError, TypeError):
            d["messages"] = []
        return d
    finally:
        conn.close()
```

settings(会话存储分区,`session_snapshot_enabled` 接在 `archive_enabled` 附近):

```python
    session_snapshot_enabled: bool = True   # 每回合冷快照(SQLite upsert),热会话过期后历史回显兜底;关=回退现状
```

- [ ] **Step 4: 跑通过** → `pytest tests/test_session_snapshot.py tests/test_db_schema.py tests/test_conversations.py -q` PASS
- [ ] **Step 5: 提交** `feat(session): S1 session_snapshots 冷快照表(upsert)+ 读写方法`

---

### Task S2(B): EcomAgent 每回合写冷快照

**Files:**
- Modify: `app/agent/chat.py`(`save()` 与回合结束 `store.save` 之后)
- Test: `tests/test_session_snapshot.py`(增)

**Interfaces:**
- Consumes: S1 的 `upsert_session_snapshot`;`self.session_path`、`self.user_id`、`self.raw_messages`、`self.summary`。
- Produces: `EcomAgent._write_snapshot()`(best-effort;session_id=`Path(session_path).stem`,空/`session` 跳过;门控 off 跳过);在 `save()` 与 `chat()` 回合结束落盘处调用。

- [ ] **Step 1: 写失败测试**(增到 `tests/test_session_snapshot.py`;裸 agent 风格,参考 test_chat_pipeline_wiring 的 `EcomAgent.__new__`)

```python
def test_ecomagent_save_writes_snapshot(tmp_path, monkeypatch):
    from app.agent.chat import EcomAgent
    from app.config.settings import settings
    from app.db import Database, set_db
    import types as _t

    db = Database(str(tmp_path / "t.db")); db.init_schema()
    set_db(db)
    monkeypatch.setattr(settings, "session_snapshot_enabled", True)
    try:
        a = EcomAgent.__new__(EcomAgent)
        a.session_path = str(tmp_path / "c-abc.json")
        a.user_id = "u1"
        a.raw_messages = [{"role": "user", "content": "你好"},
                          {"role": "assistant", "content": "您好"}]
        a.summary = None
        a._write_snapshot()
        snap = db.get_session_snapshot("c-abc")     # stem 作 session_id
        assert snap is not None and snap["user_id"] == "u1" and len(snap["messages"]) == 2
    finally:
        set_db(None)


def test_snapshot_skipped_for_default_session_name(tmp_path, monkeypatch):
    from app.agent.chat import EcomAgent
    from app.config.settings import settings
    from app.db import Database, set_db

    db = Database(str(tmp_path / "t.db")); db.init_schema()
    set_db(db)
    monkeypatch.setattr(settings, "session_snapshot_enabled", True)
    try:
        a = EcomAgent.__new__(EcomAgent)
        a.session_path = str(tmp_path / "session.json")   # 默认名 → 跳过
        a.user_id = "u1"; a.raw_messages = [{"role": "user", "content": "x"}]; a.summary = None
        a._write_snapshot()
        assert db.get_session_snapshot("session") is None
    finally:
        set_db(None)


def test_snapshot_disabled_writes_nothing(tmp_path, monkeypatch):
    from app.agent.chat import EcomAgent
    from app.config.settings import settings
    from app.db import Database, set_db

    db = Database(str(tmp_path / "t.db")); db.init_schema()
    set_db(db)
    monkeypatch.setattr(settings, "session_snapshot_enabled", False)
    try:
        a = EcomAgent.__new__(EcomAgent)
        a.session_path = str(tmp_path / "c-xyz.json"); a.user_id = "u1"
        a.raw_messages = [{"role": "user", "content": "x"}]; a.summary = None
        a._write_snapshot()
        assert db.get_session_snapshot("c-xyz") is None
    finally:
        set_db(None)
```

- [ ] **Step 2: 跑失败**

- [ ] **Step 3: 实现** `chat.py`——加方法:

```python
    def _write_snapshot(self) -> None:
        """会话冷快照(best-effort):每回合落盘后同步一份到 SQLite,热会话过期后仍可回看。

        不依赖 reaper / 进程内存态——任何会话都有永久副本。session_id 取自
        session_path 的 stem(与 RedisSessionStore 的 key 同源);默认会话名跳过。
        """
        if not settings.session_snapshot_enabled:
            return
        from pathlib import Path
        sid = Path(self.session_path).stem
        if not sid or sid == "session":
            return
        try:
            from app.db import get_db
            get_db().upsert_session_snapshot(sid, self.user_id or "default",
                                             self.raw_messages, self.summary)
        except Exception:
            pass
```

在两处落盘后调用:`chat()` 回合结束的 `self.store.save(self.session_path, self._session_state())` 之后加一行 `self._write_snapshot()`;`save()` 方法体末尾同加(`step-level _checkpoint` 的中途保存**不**写快照——避免每工具步都写 SQLite,回合末写一次即可)。

- [ ] **Step 4: 跑通过** → `pytest tests/test_session_snapshot.py tests/test_react_degrade.py tests/test_checkpoint.py tests/test_session_manager.py -q` PASS
- [ ] **Step 5: 提交** `feat(session): S2 每回合写会话冷快照(B,不依赖 reaper)`

---

### Task S3(A): 历史回显热 miss 兜底冷快照

**Files:**
- Modify: `app/api/app.py`(`/api/session/{id}/history` 端点)
- Test: `tests/test_api.py`(增)

**Interfaces:**
- Consumes: S1 的 `get_session_snapshot`;现有 `manager.peek_messages`、`reconstruct_bubbles`、U3 归属校验。
- Produces: history 端点:热存储有 → 用热;热为空 → 读快照(过归属二次校验)→ reconstruct;都无 → 空。

- [ ] **Step 1: 写失败测试**(增到 `tests/test_api.py`,用现有临时 db monkeypatch 手法)

```python
def test_history_falls_back_to_snapshot_when_hot_empty(monkeypatch):
    client, mgr = _client()   # + 临时 db monkeypatch(同 auth 测试)
    from app.db import get_db
    from app.config.settings import settings
    monkeypatch.setattr(settings, "auth_enabled", False)   # 先测纯 fallback 逻辑
    # 造:conversations 有归属 + 快照有内容 + 热存储无(peek_messages 返回 [])
    get_db().create_conversation("u1")   # 忽略返回,单独建一条已知 id
    cid = "c-snaponly"
    import sqlite3
    conn = sqlite3.connect(get_db().db_path)
    conn.execute("INSERT INTO conversations (conversation_id, user_id, status, created_at) "
                 "VALUES (?,?,?,?)", (cid, "u1", "closed", "t"))
    conn.commit(); conn.close()
    get_db().upsert_session_snapshot(cid, "u1",
        [{"role": "user", "content": "历史消息"}, {"role": "assistant", "content": "历史回复"}], None)
    r = client.get(f"/api/session/{cid}/history")
    turns = r.json()["turns"]
    assert turns and any("历史消息" in str(t) for t in turns)   # 热为空,读到了快照


def test_history_ownership_still_enforced_on_snapshot(monkeypatch):
    """auth 开时,别人的快照读不到(归属二次校验)。"""
    client, _ = _client()   # + 临时 db
    from app.db import get_db
    from app.config.settings import settings
    monkeypatch.setattr(settings, "auth_enabled", True)
    # alice 建会话+快照
    tok_a = client.post("/api/users", json={"user_id": "alice", "name": "alice"}).json()["token"]
    ha = {"Authorization": f"Bearer {tok_a}"}
    cid = client.post("/api/conversation/open", headers=ha, json={}).json()["conversation_id"]
    get_db().upsert_session_snapshot(cid, "alice", [{"role": "user", "content": "私密"}], None)
    tok_b = client.post("/api/users", json={"user_id": "bob", "name": "bob"}).json()["token"]
    hb = {"Authorization": f"Bearer {tok_b}"}
    assert client.get(f"/api/session/{cid}/history", headers=hb).status_code == 403
```

- [ ] **Step 2: 跑失败**

- [ ] **Step 3: 实现**——history 端点(在归属校验之后、返回之前):

```python
    @app.get("/api/session/{session_id}/history")
    def session_history(session_id: str, request: Request):
        uid = None
        if settings.auth_enabled:
            uid = _token_user(request)
            if uid is None:
                raise HTTPException(401, "未登录或登录已过期")
            conv = get_db().get_conversation(session_id)
            if conv is None or conv.get("user_id") != uid:
                raise HTTPException(403, "无权查看该会话")
        from app.api.history import reconstruct_bubbles
        messages = manager.peek_messages(session_id)
        if not messages and settings.session_snapshot_enabled:
            # 热存储已过期/清空 → 兜底冷快照(归属二次校验)
            try:
                snap = get_db().get_session_snapshot(session_id)
                if snap and (uid is None or snap.get("user_id") == uid):
                    messages = snap.get("messages") or []
            except Exception:
                messages = messages
        return {"session_id": session_id, "turns": reconstruct_bubbles(messages)}
```

- [ ] **Step 4: 跑通过** → `pytest tests/test_api.py tests/test_auth_enforce.py tests/test_session_snapshot.py tests/test_history_and_user_isolation.py -q` PASS
- [ ] **Step 5: 提交** `feat(session): S3 历史回显热 miss 兜底冷快照(A,归属校验保留)`

---

### Task S4: 端到端冒烟(控制方执行)

- [ ] 重启服务(redis 后端)→ 登录用户,聊 2 轮 → SQLite `session_snapshots` 出现该会话(`get_session_snapshot` 有 messages)
- [ ] **手动删 redis 热 key**(`redis-cli del sess:<cid>` 模拟 TTL 过期)→ 刷新页面 → 历史**仍可见**(走快照兜底)✅
- [ ] auth 开:B 用户 token 请求 A 的会话 history → 仍 403(归属校验对快照生效)
- [ ] 关 `SESSION_SNAPSHOT_ENABLED` 重启 → 行为回退现状(热 miss 即空);恢复
- [ ] `.superpowers/sdd/progress.md` 记账;更新根因说明(可选)

## 总量与顺序

S1(~0.3d)→ S2(~0.3d)→ S3(~0.3d)→ S4(~0.1d),共 **~1 人日**。

## Self-Review

- **覆盖核对**:B 每回合冷副本不依赖 reaper(S2,`test_ecomagent_save_writes_snapshot`)✅;A 热 miss 兜底(S3,`test_history_falls_back_to_snapshot`)✅;upsert 不堆积(S1)✅;快照/审计表分离(S1)✅;归属校验对快照生效(S3,`test_history_ownership_still_enforced_on_snapshot`)✅;门控回退(S1/S2 disabled 测试)✅;session_id 与 redis key 同源(stem)+ 默认名跳过(S2)✅。
- **占位符扫描**:S3 测试的临时 db monkeypatch"同 auth 测试"指向 tests/test_api.py 既有手法(具体、非 TBD);无 TBD。
- **类型一致性**:`upsert_session_snapshot(session_id, user_id, messages, summary)` / `get_session_snapshot -> dict(messages:list)` S1 定义、S2 写、S3 读一致;`_write_snapshot()` S2 定义并在 save/chat 调用;门控名 `session_snapshot_enabled` 贯穿。
- **已知取舍**:①快照每回合全量写 messages(不是增量)——一个会话恒一行 upsert,存储 O(1),可接受;②中途 checkpoint 不写快照(回合末写一次),崩溃在回合中途则快照是上一回合态(热存储的 checkpoint 仍是最新,快照只是过期兜底,可接受);③快照不设 TTL/清理(演示;生产可加定期清理旧快照,文档标注)。
