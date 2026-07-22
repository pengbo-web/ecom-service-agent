# 会话存储抽象 + 步级 Checkpoint — 实现方案

> 对应总方案 P2 + 存储抽象。目标:①热会话上下文**存储介质可插拔**(单机本地文件 / 生产 Redis 一行切换);②**步级 checkpoint**做到"进程中途崩溃不丢上下文、确认前重启不丢挂起动作";③补齐 nanobot 式的**有界 LRU 缓存**与**优雅停机 fsync**。
> 原则:单机 demo 用本地实现真实跑通,Redis 作为可插拔实现"留好口子",不为单机强上重依赖。

---

## 1. 现状与问题

| 现状 | 问题 |
|---|---|
| 会话上下文在 `EcomAgent.raw_messages`(内存)+ 每**回合末** `save_session` 到 `app/sessions/api/{sid}.json` | ReAct **回合中途崩溃 → 本轮丢失** |
| `SessionManager._agents` dict 缓存,无上限 | 高并发多会话**内存无界** |
| `PendingActionStore` 纯内存 | **确认前重启 → 挂起动作丢失**,用户要重发 |
| 存储写死本地 JSON | 多实例横向扩展时无法共享(需 Redis/共享存储) |
| 无 fsync | 网络盘/优雅停机时最新写可能丢 |

## 2. 总体设计

```
SessionManager(有界 LRU + weakref 溢出)
   │  get_or_create / save / checkpoint
   ▼
SessionStore(抽象接口)
   ├── FileSessionStore   ← 默认(单机):原子 tmp+replace,可选 fsync
   └── RedisSessionStore  ← 生产(多实例):hot key + TTL,可插拔
```

四块改动:**(A) SessionStore 抽象 + FileSessionStore**、**(B) 有界 LRU 缓存**、**(C) 步级 checkpoint + 恢复**、**(D) 挂起动作持久化**;**(E) Redis 实现**与**(F) 优雅停机 fsync** 为可选增强。

## 3. 接口设计

```python
# app/session/store.py(新)
class SessionState(TypedDict):
    version: int
    messages: list[dict]
    summary: str | None
    short_term_memory: dict | None
    status: str            # "complete" | "in_flight"
    step_seq: int          # 本回合已 checkpoint 的步数(崩溃定位)
    pending: dict | None   # 挂起的待确认动作(action/tool/args/message)
    updated_at: str

class SessionStore(Protocol):
    def load(self, session_id: str) -> SessionState | None: ...
    def save(self, session_id: str, state: SessionState, *, fsync: bool = False) -> None: ...
    def delete(self, session_id: str) -> None: ...
```

`storage.save_session/load_session` 收敛为 `FileSessionStore` 的实现(保留原子写),对外统一走 `SessionStore`。工厂 `get_session_store()` 据 `settings.session_store_backend`("file"|"redis")返回实现。

## 4. 数据模型变化(session 文件)

在现有 `{version, messages, summary, short_term_memory}` 基础上加:
- `status`:`in_flight`(回合进行中)/ `complete`(回合结束)。
- `step_seq`:本回合内已完成的工具步数(恢复时定位)。
- `pending`:挂起的待确认动作(替代纯内存的 PendingActionStore,或双写)。

向后兼容:老文件无这些字段 → 读时默认 `status=complete, step_seq=0, pending=None`。

## 5. 步级 Checkpoint 与恢复语义(核心)

### 写入时机
- **回合开始**:先持久化用户消息 + `status=in_flight, step_seq=0`(借 nanobot `_persist_user_message_early`)。
- **每个工具步后**(`_execute_tool_call` 末尾):把最新 raw_messages + `step_seq+=1` 增量落盘(仍整文件原子重写,文件小、代价可忽略)。
- **回合结束**:写结构化回复 + `status=complete`,清 `pending`(若已落地)。

### 恢复语义(分两级,重点讲清幂等)

**Level 1 — 持久化(推荐,安全,先做)**
- 装载时若 `status=in_flight`:**上下文完整恢复**(到最后一次 checkpoint),解决"重启丢会话"。
- **不自动重放工具**:对崩溃时"有 tool_call 无 tool_result"的孤儿调用,用现有 `sanitize_tool_pairs` 丢弃 → 交下一轮由模型/用户重新决定。
- **挂起动作恢复**:`pending` 已落盘 → 用户"确认"仍能触发服务端重放(不丢)。
- **为什么不自动重放**:写操作(退款/取消)可能已执行但结果没落盘,盲目重放会**双重执行**。安全第一:只保证不丢上下文/不丢挂起,不赌工具重放。

**Level 2 — 自动续跑(可选,高阶)**
- 给写工具加**幂等键** `idempotency_key = hash(session_id, step_seq, tool, args)`:执行前查"该键是否已应用",已应用则直接返回上次结果(不重复副作用)。
- 有幂等键后,才可安全**自动续跑**被中断的回合(从 `step_seq` 继续)。
- 依赖:DB 加 `applied_actions(idempotency_key, result, ts)` 表。工作量更大,作为后续增强。

## 6. 有界 LRU 缓存(借 nanobot)

`SessionManager._agents` 改为:
- `_cache: OrderedDict`(强引用,LRU,上限 `settings.session_cache_max`,默认 200),超限淘汰最久未用;
- `_overflow: WeakValueDictionary`(弱引用溢出,活跃调用方持有的不丢、闲置的可回收)。
- 淘汰前确保已 `save`(不丢盘);与现有 idle-reaper 协同(reaper 负责巩固+回收,LRU 负责内存上限)。

## 7. Redis 实现(可选,生产,留口子)

`RedisSessionStore`(`redis-py`,`import` 守卫,缺库时报清晰错误):
- key `sess:{session_id}` 存 `SessionState`(JSON),`SETEX` 带 TTL(`settings.session_ttl`,默认 3600s)。
- 冷归档:巩固/结束时可同时落 DB/对象存储(附录,非本方案)。
- 切换:`SESSION_STORE_BACKEND=redis` + `REDIS_URL` 即启用,业务代码零改动。

## 8. 优雅停机 fsync(借 nanobot)

`FileSessionStore.save(fsync=True)`:写完 `flush()+os.fsync()`,并 fsync 父目录(Windows 上 `PermissionError` 时跳过,NTFS 元数据同步写)。在进程 `SIGTERM`/FastAPI `shutdown` 事件里对所有活跃会话 `save(fsync=True)`——防网络盘/优雅停机丢最新写。

## 9. 配置(settings)

```
session_store_backend: str = "file"     # file | redis
session_cache_max: int = 200            # 内存强缓存会话上限(LRU)
session_ttl: int = 3600                 # redis 会话过期(秒)
redis_url: str = ""                     # redis 连接串
checkpoint_enabled: bool = True         # 步级 checkpoint 开关
```

## 10. 分阶段任务(每阶段独立提交、离线全绿)

| 阶段 | 内容 | 改动文件 | 验收 | 工作量 |
|---|---|---|---|---|
| **C1** | SessionStore 抽象 + FileSessionStore(收敛现有 storage)| 新 `app/session/store.py`;`storage.py` 复用;`chat.py`/`session_manager.py` 改走 store | 行为等价,全量离线绿 | 1d |
| **C2** | 有界 LRU + weakref 缓存 | `session_manager.py` | 超上限淘汰且淘汰前落盘;单测(注入小上限)| 1d |
| **C3** | 步级 checkpoint + Level 1 恢复 | `chat.py`(每步落盘+in_flight)、`store.py`(字段)、`session_manager.py`(装载识别 in_flight)| 模拟回合中途 kill,重启后上下文不丢;孤儿 tool_call 被清;单测 | 2d |
| **C4** | 挂起动作持久化 | `pending.py`(双写 store)、`store.py` | 确认前重启,重启后"确认"仍能重放;单测 | 1d |
| **C5** | 优雅停机 fsync | `store.py`、`run_api.py`(shutdown 钩子)| SIGTERM 时活跃会话 fsync 落盘 | 0.5d |
| **C6**(可选)| RedisSessionStore + 配置切换 | 新 `RedisSessionStore`、settings | `SESSION_STORE_BACKEND=redis` 可跑(需本地 redis);file 默认不受影响 | 1.5d |
| **C7**(可选高阶)| 幂等键 + Level 2 自动续跑 | 写工具、DB `applied_actions`、`chat.py` 续跑 | 中断回合自动续跑且不双重执行;单测 | 2.5d |

**核心(C1–C5)约 5.5 人日**;可选(C6/C7)按需。

## 11. 测试策略

- **C1**:store 读写往返、原子写、老文件兼容。
- **C2**:注入 `session_cache_max=2`,访问 3 个会话 → 最久的被淘汰且已落盘、再访问能从盘重载。
- **C3**:构造"回合中途"状态(in_flight + 半截 messages)→ 装载后上下文完整、孤儿 tool_call 被 `sanitize_tool_pairs` 清除;`step_seq` 正确。
- **C4**:remember 挂起 → 重建 store → get 仍在 → 重放成功清除。
- 全部用**可注入时钟/临时目录/fake**,不触网。

## 12. 面试点

> "会话存储我做了 `SessionStore` 抽象:单机用本地原子文件 + 有界 LRU/weakref 缓存(等价 nanobot),生产多实例一行切 Redis(hot key + TTL)。可中断可恢复用**步级 checkpoint**——每个工具步落盘 + `in_flight` 标记,重启后上下文与挂起动作都不丢;对写操作我**不盲目重放**(防双重执行),Level 2 用**幂等键**才安全自动续跑。优雅停机走 fsync 防网络盘丢写。介质、缓存、持久化粒度三者解耦。"
