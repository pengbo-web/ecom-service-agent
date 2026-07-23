# 会话存储(Redis)+ 步级 Checkpoint — 技术方案

> **目标**:以 **Redis** 作为热会话上下文存储(快、可共享、带 TTL),配合**步级 checkpoint** 实现"进程/实例中途崩溃不丢上下文、确认前重启不丢挂起动作",并支持**多实例横向扩展**。
> **本地可跑**:开发用 Docker 起 Redis;离线测试用 `fakeredis`(内存假实现,不需真服务、不触网)。保留 `SessionStore` 抽象,`file` 仅作降级/极简本地用。

---

## 1. 为什么是 Redis + checkpoint(而非本地文件)

| 能力 | 本地文件 + 进程内缓存 | **Redis + checkpoint** |
|---|---|---|
| 多实例共享热会话 | ❌ 请求打到别的实例就断上下文 | ✅ 所有实例读同一份 |
| 会话过期回收 | 需自建 reaper | ✅ 原生 TTL |
| 读写延迟 | 磁盘 | ✅ 内存级 |
| 崩溃/重启恢复 | 回合末才落盘,中途丢 | ✅ 步级 checkpoint,任意实例可恢复 |
| 并发安全(同会话) | 进程内 `threading.Lock`(仅单实例有效) | ✅ **分布式锁**(跨实例) |
| 持久化 | 文件即持久 | Redis **AOF** 持久化 + 冷归档 |

结论:面向"生产级、可横向扩展"的目标,**Redis 是热会话的正确介质**;checkpoint 解决"崩溃恢复粒度";两者配合,任意实例都能从 Redis 恢复被中断的会话。

## 2. 总体架构

```
       多实例(N 个 app 进程,LB 后)
   inst-A        inst-B        inst-C
      \            |            /
       \           |           /
        ▼          ▼          ▼
     ┌─────────────────────────────┐
     │            Redis            │  ← 热会话上下文 + 步级 checkpoint + 分布式锁 + 挂起动作
     │  sess:{id}(state, TTL)      │     AOF 持久化(everysec)
     │  lock:{id}(SET NX PX)       │
     └─────────────────────────────┘
                    │ 会话结束/空闲(异步)
                    ▼
        冷归档:SQLite/DB/对象存储(长期留存、审计、离线分析)
```

## 3. 数据模型(Redis Key 设计)

| Key | 类型 | 内容 | TTL |
|---|---|---|---|
| `sess:{session_id}` | String(JSON) | `SessionState`:messages / summary / short_term_memory / **status** / **step_seq** / **pending** / updated_at | `session_ttl`(默认 3600s,每次访问续期) |
| `lock:{session_id}` | String | 持有者 token(`SET NX PX`) | 锁超时(默认 30s) |

`SessionState`(与介质无关的统一结构):
```python
class SessionState(TypedDict):
    version: int
    messages: list[dict]
    summary: str | None
    short_term_memory: dict | None
    status: str            # "complete" | "in_flight"
    step_seq: int          # 本回合已 checkpoint 的工具步数
    pending: dict | None   # 挂起待确认动作(action/tool/args/message)
    updated_at: str
```

### 3.1 `sess:{session_id}` 的值示例(退款·确认前)

以"用户 alice 要退款、正处于确认前"为例,`GET sess:alice--web123` 拿到的 JSON:

```json
{
  "version": 1,
  "status": "in_flight",
  "step_seq": 1,
  "updated_at": "2026-07-22T10:30:05",
  "summary": null,
  "messages": [
    {"role": "user", "content": "我要退款订单 ORD-20240115-001,尺码不合适"},
    {"role": "assistant", "content": null,
     "tool_calls": [{"id": "call_1", "type": "function",
       "function": {"name": "apply_refund",
                    "arguments": "{\"order_id\":\"ORD-20240115-001\",\"reason\":\"尺码不合适\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1",
     "content": "{\"success\":false,\"need_confirm\":true,\"action\":\"refund\",\"message\":\"退款是敏感操作,请确认…\"}"}
  ],
  "short_term_memory": {"facts": ["用户关注订单 ORD-20240115-001", "退款原因:尺码不合适"]},
  "pending": {
    "action": "refund", "tool_name": "apply_refund",
    "args": {"order_id": "ORD-20240115-001", "reason": "尺码不合适"},
    "message": "退款是敏感操作,请确认是否为订单 ORD-20240115-001 办理退款?"
  }
}
```

字段职责:`messages` 是真·对话上下文(恢复靠它);`status/step_seq` 是 checkpoint 元信息(in_flight=上轮没跑完、定位到第几步);`pending` 是挂起动作(下轮"确认"时任意实例读到即可服务端重放);`summary` 历史压缩;`short_term_memory` STM(长期记忆单独存/归档,不在此)。

### 3.2 一次退款回合的 Key 演变(checkpoint 逐步写)

```
轮1「我要退款…」
 ├─ 回合开始 → SET sess:{id} {status:in_flight, step_seq:0, messages:[user]}
 ├─ 调 apply_refund 得 need_confirm → SET {step_seq:1, messages:[user,assistant,tool], pending:{refund…}}
 └─ 回合结束 → SET {status:complete, messages:[…,assistant回复]}   // pending 保留
轮2「确认退款」
 ├─ 抢锁 lock:{id} → 命中 pending → 服务端重放 apply_refund → 成功
 ├─ SET {status:complete, pending:null, messages:[…]}   // 清挂起
 └─ 释放锁
```

任意一步后 Redis 崩溃/实例重启:因每步都 SET 了最新状态(+AOF),另一实例 `GET sess:{id}` 就能拿完整现场续上。

### 3.3 为什么用 String(JSON) 而非 Hash / Stream

- **String(整块 JSON)**:一次 GET/SET 读写整会话,简单、单键原子、TTL 好管——**本方案采用**。
- **Hash(每字段一 field)**:可只更新某字段省带宽,但会话本就整体读给模型,收益小、命令多。
- **List/Stream(消息逐条 append)**:适合超长历史只追加;代价是读时拼装 + 单独管元数据。**历史达几百条时可升级**,当前 String 足够。

## 4. 组件设计

### 4.1 SessionStore 抽象 + RedisSessionStore
```python
# app/session/store.py
class SessionStore(Protocol):
    def load(self, session_id: str) -> SessionState | None: ...
    def save(self, session_id: str, state: SessionState) -> None: ...   # SETEX 带 TTL
    def delete(self, session_id: str) -> None: ...

class RedisSessionStore:
    def __init__(self, url: str, ttl: int): self._r = redis.from_url(url); self._ttl = ttl
    def load(self, sid):  raw = self._r.get(f"sess:{sid}"); return json.loads(raw) if raw else None
    def save(self, sid, state): self._r.set(f"sess:{sid}", json.dumps(state, ensure_ascii=False), ex=self._ttl)
    def delete(self, sid): self._r.delete(f"sess:{sid}")
```
- 工厂 `get_session_store()` 据 `settings.session_store_backend` 返回 `RedisSessionStore`(生产)/ `FakeRedisSessionStore`(测试)/ `FileSessionStore`(降级)。
- `EcomAgent` 的 load/save 改为走 store(不再直接 `save_session` 到本地文件)。

### 4.2 分布式会话锁(多实例正确性,关键)
单实例的 `threading.Lock` 在多实例下失效——两个实例可能同时处理同一会话、互相覆盖 Redis 状态。改用 **Redis 分布式锁**:
```python
# 获取:SET lock:{sid} <token> NX PX <lock_ms>   成功才处理
# 释放:Lua 校验 token 再 DEL(防误删别人的锁)
```
- `/api/chat` 处理某会话前先抢锁,拿不到 → 排队/短暂重试/提示"处理中"。
- 锁带超时(防实例崩溃后死锁);长回合可续租(watchdog)。
- 单实例部署时锁退化为本地即可(可用同一接口,Redis 版天然兼容)。

### 4.3 步级 Checkpoint(写入时机)

**Checkpoint 是什么**:在会话进行的每一步都把状态存下来,而不是等整轮结束才存一次——像游戏边打边存档,而非通关才存。

**主要解决什么问题**:一轮客服对话不是瞬间完成的,ReAct 循环里会多次调 LLM + 多次调工具,耗时数秒甚至更久:

```
用户"退款" → LLM思考 → 调 query_order → LLM思考 → 调 apply_refund → LLM组织回复 → 落盘
                                   ↑ 若这中间进程崩了/重启了/部署了…
```

- **改之前(只在回合末落盘)**:中间任何一步崩溃,因为还没到"落盘",**整轮全丢**——用户说的话、已查到的订单、已跑的工具,重启后全没了,得从头再来。
- **Checkpoint 后**:每步都存档,崩在哪都能从最近存档点恢复现场。

**效果(before / after)**:

| 场景 | 改之前 | 改之后(checkpoint) |
|---|---|---|
| 回合中途进程崩溃/重启 | 整轮丢失,用户要重说 | 上下文完整恢复,已查数据不丢 |
| 部署/重启碰上正在进行的对话 | 中断丢失 | 恢复后接着聊 |
| 多实例(配 Redis) | 换实例=断 | 任意实例从存档点接续 |
| Redis 里能看到 | `status:None` | `status:in_flight/complete`、`step_seq:N` 实时反映跑到第几步 |

**关键安全边界**:checkpoint 保证不丢上下文,但**不自动重放工具**——若崩溃前刚跑完退款,盲目重跑会退两次款。故 L1 语义 = 恢复现场、接着对话,但不自动重执行有副作用操作;要"自动续跑且不重复扣款"须给写操作加幂等键(L2,见 §4.4)。

**与"存哪"的区别**:存哪(Redis/文件)是介质;checkpoint 是**存的频率/粒度**(每步 vs 每轮),两者独立、可叠加(本方案 = Redis + 步级 checkpoint)。

**何时感受得到**:平时顺畅一轮感受不到(后台存档);崩溃/重启/部署时才显现——对话不凭空消失。它也是 §4.5 挂起动作持久化的地基。一句话:**checkpoint 让"进行到一半的对话"具备抗崩溃能力**,是"能 demo"与"敢上线"的分界之一。

**写入时机(实现)**:
- **回合开始**:持久化用户消息 + `status=in_flight, step_seq=0` → `save` 到 Redis。
- **每个工具步后**(`_execute_tool_call` 末尾):最新 messages + `step_seq+=1` → `save`(Redis SET,内存级、极快,步级落盘无压力)。
- **回合结束**:写结构化回复 + `status=complete`,清 `pending`(若已落地)。
- Redis 开 **AOF(appendfsync everysec)**:即使 Redis 进程崩溃,最多丢 1s 写,checkpoint 基本不丢。

### 4.4 恢复语义(核心,分两级)
装载会话时若 `status=in_flight`(说明上次回合被中断):

**Level 1 — 持久化恢复(先做,安全)**
- **上下文完整恢复**到最后一次 checkpoint(任意实例都能从 Redis 读到)。
- **不自动重放工具**:崩溃时"有 tool_call 无 tool_result"的孤儿调用,用现有 `sanitize_tool_pairs` 清除 → 交下一轮重新决定。
- **挂起动作恢复**:`pending` 在 Redis state 里 → 用户"确认"仍触发服务端重放(跨实例也不丢)。
- 理由:写操作(退款/取消)可能已执行但结果没落盘,盲目重放会**双重执行**;安全第一。

**Level 2 — 自动续跑(可选高阶)**
- 写工具加**幂等键** `idempotency_key = hash(session_id, step_seq, tool, args)`,执行前查 Redis `applied:{key}` 是否已应用,已应用直接返回上次结果(不重复副作用)。
- 有幂等键后才安全**从 `step_seq` 自动续跑**被中断的回合。

### 4.5 挂起动作持久化
`PendingActionStore` 不再纯内存:挂起动作写进 `SessionState.pending`(随 Redis 落盘)。任意实例、重启后,用户"确认"都能读到并重放。

### 4.6 冷归档(长期留存/审计)
Redis 是热存储(带 TTL 会过期)。会话结束/空闲时,异步把完整会话 + 长期记忆归档到 **SQLite/DB/对象存储**(审计、离线分析、数据飞轮用)。热读走 Redis,冷读走归档。

## 5. 本地开发 / 测试路径(保证单机能跑、测试不触网)

- **本地运行**:`docker-compose` 起一个 Redis(附 compose 片段);`REDIS_URL=redis://localhost:6379/0`。
- **离线测试**:用 **`fakeredis`**(纯内存假实现)注入 `RedisSessionStore`,单测无需真 Redis、不触网,全量离线套件照常绿。
- **降级**:未配 Redis 时可 `SESSION_STORE_BACKEND=file` 退回本地文件(极简本地/无 Docker 环境用)。

## 6. 配置(settings)

```
session_store_backend: str = "redis"    # redis | file | fake(测试)
redis_url: str = "redis://localhost:6379/0"
session_ttl: int = 3600                 # 热会话过期(秒),每次访问续期
session_lock_ms: int = 30000            # 分布式锁超时
checkpoint_enabled: bool = True         # 步级 checkpoint 开关
archive_enabled: bool = True            # 会话结束冷归档
```
`requirements.txt` 加 `redis>=5.0`;测试依赖 `fakeredis`。

## 7. 分阶段任务(每阶段独立提交、离线全绿)

| 阶段 | 内容 | 改动文件 | 验收 | 工作量 |
|---|---|---|---|---|
| **R1** | SessionStore 抽象 + RedisSessionStore + 工厂 + fakeredis 测试 | 新 `app/session/store.py`;`chat.py`/`session_manager.py` 走 store;settings | fakeredis 读写往返;`chat` 读写会话经 Redis;全量离线绿 | 1.5d |
| **R2** | 步级 checkpoint + L1 恢复 | `chat.py`(每步 save+in_flight)、`store.py` | 模拟回合中途"崩溃"(留 in_flight 态)→ 新建 store 装载上下文不丢、孤儿 tool_call 被清;单测 | 2d |
| **R3** | 挂起动作持久化(入 SessionState.pending) | `pending.py`、`streaming.py`、`store.py` | 确认前"重启"(重建 store)→ "确认"仍能重放;单测 | 1d |
| **R4** | 分布式会话锁(Redis SET NX PX + Lua 释放) | 新 `app/session/lock.py`;`app.py` 抢锁 | 并发两次同会话请求被串行化;锁超时释放;fakeredis 单测 | 1.5d |
| **R5** | 冷归档 + Redis AOF 说明 + docker-compose | 归档器、`docker-compose.yml`、docs | 会话结束落归档;文档给 AOF 配置与 compose | 1d |
| **R6**(可选高阶) | 幂等键 + L2 自动续跑 | 写工具、Redis `applied:{key}`、`chat.py` | 中断回合自动续跑且不双重执行;单测 | 2.5d |

**核心 R1–R5 约 7 人日**;R6 可选。

## 8. 测试策略(全部 fakeredis / 临时目录 / 可注入时钟,不触网)

- **R1**:store CRUD 往返、TTL 参数、老状态兼容;`EcomAgent` 经 store 读写。
- **R2**:构造 in_flight + 半截 messages 的 Redis state → 装载后上下文完整、`step_seq` 正确、孤儿 tool_call 被 `sanitize_tool_pairs` 清。
- **R3**:remember 挂起 → 重建 store(模拟重启)→ pending 仍在 → 重放清除。
- **R4**:两个"实例"抢同一 `lock:{sid}` → 只有一个成功;释放后另一个可得;超时自动释放。
- 不引入真 Redis 到 CI;`fakeredis` 覆盖行为。

## 9. 面试点

> "热会话上下文我放 **Redis**:`sess:{id}` 存状态带 TTL 自动过期,多实例共享——横向扩展时任意实例都能接续同一会话。可中断可恢复用**步级 checkpoint**:每个工具步写回 Redis + `in_flight/step_seq` 标记,配合 Redis AOF 持久化,崩溃后任意实例从 Redis 恢复;写操作**不盲目重放**(防双重执行),Level 2 用幂等键才自动续跑。多实例并发用 **Redis 分布式锁**(SET NX PX + Lua 安全释放)替代进程内锁。热存 Redis、冷归档到 DB。本地用 Docker Redis、测试用 fakeredis,单机可跑、CI 不触网。介质 / 缓存 / 持久化粒度 / 并发控制四者解耦。"
