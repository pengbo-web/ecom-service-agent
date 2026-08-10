# PostgreSQL 迁移方案（队列 + 业务库）

**Goal:** 让系统能在**多实例**下运行——这是 SQLite 唯一真正拦住的事。协作队列用
`FOR UPDATE SKIP LOCKED`，业务数据与队列**仍在同一个库、同一个事务**，现有的优先级
调度、事件三态状态机、按 `correlation_id` 的全链路回溯**零损失**。

---

## 一、先说清一件事：不要只迁队列

「换成 PG 队列」这个说法有歧义，两种做法差别很大：

| 做法 | 结果 |
|---|---|
| **只把 `agent_events` 迁到 PG** | ❌ 业务数据仍在 SQLite → 发布事件与写业务数据**跨库**，失去同事务保证，要么双写要么上分布式事务；同时多一个运维组件，而 SQLite 的单写者限制仍然卡着下单/退款/审批主链路 |
| **业务库整体迁 PG，队列跟着走** | ✅ 队列只是其中一张表；`publish` 与业务写在同一事务里；一次性解决多实例问题 |

**`agent_events` 与订单、商品、会话、草稿本来就在同一个库**（`app/sessions/ecom.db`）。
多实例部署时**先撞墙的是业务库不是队列**——只迁队列是本末倒置，还会把一个原本
干净的"同库同事务"结构拆成双库。

**本方案采用第二种。** 队列作为**第一个切片**先迁、先验证，但目标是整库。

---

## 二、工作量实测（不是估算，是数出来的）

| 项 | 数量 | 说明 |
|---|---|---|
| `app/db/database.py` | 2009 行 / 96 个方法 | 主体 |
| 占位符 `?` | 245 处 | PG 用 `%s` |
| `AUTOINCREMENT` | 12 处 | PG 用 `GENERATED ... AS IDENTITY` |
| `datetime('now', '-N hours')` | 9 处（+ 其它模块 16 处） | PG 用 `now() - interval 'N hours'` |
| `ON CONFLICT ... DO UPDATE` | 6 处 | **PG 原生支持，不用改** ✅ |
| 部分唯一索引 `WHERE status='draft'` | 2 处 | **PG 原生支持，不用改** ✅ |
| `PRAGMA` | 14 处 | 全部删除（WAL/busy_timeout 是 SQLite 概念） |
| `sqlite3.IntegrityError` 等异常 | 8 处 | 换 `psycopg.errors.UniqueViolation` |
| `cur.lastrowid` | 5 处 | PG 用 `INSERT ... RETURNING id` |
| **`database.py` 之外还写 SQL 的模块** | **13 个** | growth(7 execute/13 datetime)、observability/store(9)、memory/fts_store(12)、hitl/queue(5)、shop_analytics(6)、followup(6)… |

**两处真正的坑**（不是机械替换）：

1. **`memory/fts_store.py` 用了 SQLite FTS5 虚拟表**。PG 没有 FTS5。
   好消息：读了代码，它**实际不依赖 MATCH**——注释写明「FTS5 默认分词器对中文
   几乎不可用，因此无论后端是否 FTS5，`search` 一律走方案①（Python 侧过滤）」。
   所以迁移时 FTS5 表退化成普通表即可，召回逻辑一行不用改。
   （将来要真全文检索，PG 的 `pg_trgm` 或 `zhparser` 比 FTS5 更适合中文。）
2. **三个独立 SQLite 文件**：`ecom.db`（业务+队列）、`traces.db`（观测）、
   `hitl.db`（工单）。迁 PG 后应合并成一个库的不同 schema——尤其 `hitl.db`，
   消费闸要读它的 `count_pending()`，跨库读是当前一个隐性耦合。

---

## 三、Global Constraints

1. **行为零变化**：迁移期间所有语义必须逐字节保持——幂等条件更新、优先级排序、
   三态状态机、部分唯一索引、fail-soft 边界。**这是一次基础设施替换，不是重构机会。**
2. **两套后端并存一段时间**，由配置切换。不允许"改完直接上"——1650 个测试必须
   在两套后端上都绿。
3. **不动安全边界**：consent 门、人工审批闸、工具子集一律不改。
4. **不趁机改表结构**。迁移与建模同时做，出问题时无法二分定位。
5. 中文注释；全量保持全绿。

---

## Task PG1: 抽 SQL 方言层（**先做，不碰 PG**）

**Files:** `app/db/dialect.py`(新增)、`app/db/database.py`、测试

在引入 PG 之前，先把 SQLite 专有语法收敛到一处。这一步**单独就有价值**（今天就能
合并），而且做完之后 PG 适配才是可控的。

- 占位符：统一用 `%s` 风格 + 一个转换函数，或改用命名参数
- 时间表达式：`_now_minus(hours)` / `_elapsed_hours(col)` 取代散落的
  `datetime('now', ...)` 与 `julianday(...)`
- 自增主键取回：统一走 `RETURNING`（SQLite 3.35+ 也支持，先在 SQLite 上验证）
- 异常类型：`IntegrityError` 收敛成项目自己的 `DuplicateKey` 异常

**验收**：全量在 SQLite 上仍全绿；`grep "datetime('now'"` 在 `app/` 下归零。

---

## Task PG2: `PostgresEventBus`（队列切片）

**Files:** `app/multi_agent/event_bus.py`、测试

队列是**最适合当试点的切片**：接口已经抽好（`EventBus` Protocol），语义契约已写清，
而且它的正确性有最强的测试覆盖（幂等认领、优先级、三态、扇出、滞留回收）。

核心改动只有 `claim`：

```sql
-- SQLite：先 SELECT 再逐行条件 UPDATE
-- PG：一条语句搞定，且天生支持多实例
UPDATE agent_events SET status='processing', consumed_at=now()
WHERE id IN (
    SELECT id FROM agent_events
    WHERE target_agent = %s AND status = 'pending'
    ORDER BY priority DESC, id ASC
    LIMIT %s
    FOR UPDATE SKIP LOCKED        -- ← 多实例安全的关键
)
RETURNING *;
```

`SKIP LOCKED` 让并发 worker 各取各的、互不阻塞——这正是 SQLite 做不到而 Kafka
也提供不了优先级的那个位置。其余方法（`finish`/`reclaim_stale`/`timeline`/
`failed`/`retry`）都是直译。

**验收**：把 `tests/test_collab_routing.py` / `test_collab_rule_engine.py` /
`test_agent_bus.py` 参数化跑两套后端，断言完全一致。多进程并发认领无重复。

### ✅ PG2 已完成（2026-08-10 实跑验收）

**环境**：`psycopg 3.3.4`（清华源安装，绕过本机代理）；PG 容器
`ecom-pg`（`postgres:15-alpine`，端口 **5442** —— 5432 被 `aperag-postgres` 占着），
`restart=unless-stopped`（与 `ecom-redis` 对齐，避免像 ApeRAG 那样重启后躺着）。

**新增**：`app/multi_agent/pg_event_bus.py`、开关 `collab_bus_backend` /
`collab_pg_dsn`（默认 `sqlite`）、`tests/test_pg_event_bus.py`。

**验收结果**：`38 passed` —— 语义断言**参数化跑两套后端、结果完全一致**
（发布/认领往返、target 隔离、优先级降序 + 同优先级 FIFO、limit、不重复认领、
三态终结、条件更新幂等、failed 列表与计数、retry 幂等、滞留回收、`consumed_at` 清空、
时间线过滤与倒序、时间戳归一化为字符串）。

PG 专属两条：

- **6 线程 × 60 事件并发认领：零重复、零丢失。** 这是迁 PG 的全部理由——SQLite 版
  "先 SELECT 再逐行条件 UPDATE" 在多实例下会 SELECT 到同一批 id 再互相抢，
  抢输的 rowcount=0 白跑一圈，而且**不报错**，只表现为"worker 在跑但吞吐上不去"。
- 结构断言钉住 SQL 里真有 `FOR UPDATE SKIP LOCKED` 与 `priority DESC, id ASC`
  ——小数据量下并发断言即使没有 SKIP LOCKED 也可能偶然通过（行锁排队而非跳过），
  两条一起才说明并发安全来自机制而非运气。

**上层零改动**：打开开关后走门面 `publish → consume → timeline` 一次跑通
（`{'claimed': 1, 'done': 1, 'failed': 0}`），协作 worker 与 API 一行未改。

**工厂降级**：配成 `pg` 但 DSN 缺失/连不上时**回落 SQLite 并记 warning，不抛**。
队列是买家会话热路径上 `publish` 的落点（客服侧发 `service_escalation` 旁路信号），
让一个配置问题打死买家链路不成比例——与会话存储/会话锁同一姿态（那两处已验证过：
Redis 一停曾让每条买家消息变成 500）。warning 里写明"降级期间队列可用但**不具备
多实例安全**"，否则运维只会看到吞吐上不去而不知道原因。

**默认仍是 sqlite，这是刻意的**：只迁队列意味着 `publish` 与业务写不在同一事务里。
这个开关现阶段的用途是验证 PG 路子与多实例认领，不是"今天就切过去"——真正切换的
前置是 PG3。

---

## Task PG3: `PostgresDatabase`（业务库主体）

**Files:** `app/db/`、13 个写 SQL 的模块、测试

按表分组推进，每组独立可验证：

| 组 | 表 | 风险 |
|---|---|---|
| A | products / orders / order_items / users | 低，纯 CRUD |
| B | conversations / session_snapshots / session_archive | 中，状态机 |
| C | outreach_drafts / outreach_followups / coupon_grants | **高**：部分唯一索引 + 条件更新幂等，是人工闸的账本 |
| D | agent_events / shared_context / worker_heartbeats | 已在 PG2 完成 |
| E | skill_traces / turn_signals / reviews / carts | 低 |

### 🟡 PG3 进行中（2026-08-10：地基完成 + 建表通过，业务方法逐组推进）

**做法上有一处偏离原方案，是刻意的**：原计划写一个平行的 `PostgresDatabase` 类。
实际改成**让现有 `Database` 参数化**（`backend` / `dsn` / `self.d`）。

理由：平行类意味着把 96 个方法抄第二遍，而两份实现必然漂移——本项目已经反复吃过
"一半组件做对、另一半漏了"的亏（会话锁降级、`list_user_orders` 的 success 判定、
conftest 漏钉 `mcp_enabled`、ApeRAG 漏了代理绕行）。参数化之后 SQL 只有一处，
方言差异收敛在 `dialect.py`，驱动差异收敛在 `pg_conn.py`。

#### 已完成

| 项 | 内容 |
|---|---|
| 方言对象化 | `SqliteDialect` / `PostgresDialect` + `get_dialect()`。**不用模块级 `set_backend()`**——双后端参数化测试必须同时持有两个实例，全局可变状态会让两边互相打断，且症状随执行顺序漂移 |
| 占位符转换 | `pg_conn.qmark_to_format`：`?` → `%s`，**只翻占位符不动字符串字面量**。PG1 说的"在 execute 边界做一次"就是这里 |
| 建表翻译 | `dialect.translate_schema`：AUTOINCREMENT → identity、`INTEGER`/`REAL` → `bigint`/`double precision`。**后处理而非把 22 张表改成 f-string**——那要在 300 行重复 DDL 里插 30 多处 `{...}`，且每次加表都得记着，漏一次就是只在 PG 上炸的错误 |
| 旧库列补齐 | 整段 88 行 `PRAGMA table_info` + ALTER 包进 `if not self.is_pg`（PG 是全新库，建表就带全部列；`PRAGMA` 在 PG 上是语法错误） |
| **建表实跑** | ✅ 真 PG 上 **22 张表 / 40 个索引**建成，含部分唯一索引（PG 原生支持，一个字没改） |
| 业务方法实跑 | ✅ 13 个核心方法在 PG 上 11 个直接通过；两处真实差异已修（见下） |
| 双后端测试 | `tests/test_pg_database.py` **21 passed**（PG 档 + SQLite 档全绿）；`test_pg_conn_translate.py` 14 passed |

#### 实跑撞到的三处**不是机械替换**的差异

1. **`orders.user` —— `user` 是 PG 保留字**，裸写 `syntax error at or near "user"`。
   **不改列名**（项目里约 130 处，SQL 与 Python 混杂，而 PG3 的约束是"行为零变化、
   这是基础设施替换不是重构机会"）。改成 PG 侧加双引号：`"user"` 在 PG 与 SQLite
   里都是合法标识符，**两套后端仍共用一份 SQL**。DDL 在 `translate_schema` 处理，
   DML 在连接层 `quote_reserved` 处理。
2. **`SUM(status = 'failed')` 在 PG 上报 `function sum(boolean) does not exist`。**
   SQLite 把布尔当 0/1，PG 严格区分。加 `dialect.count_if()`。
   这类差异**不会在建表时暴露**，只在跑到那条查询时才炸——所以值得占一个方言函数，
   而不是在调用点各写一遍 CASE。
3. **`group_concat` → `string_agg(col, ',')`**，PG 必须显式给分隔符。分隔符必须与
   SQLite 一致：调用方按 `,` split，换个分隔符不会报错，只会让协作链上的 Agent
   列表显示成一坨。

#### 还没做（诚实清单）

- **13 个 `database.py` 之外写 SQL 的模块**：growth.py（7 execute / 13 datetime）、
  observability/store.py（9）、memory/fts_store.py（12）、hitl/queue.py（5）、
  shop_analytics.py（6）、followup.py（6）… 它们仍直接用模块级方言函数（只产
  SQLite 片段），走 PG 会失败。
- **FTS5 虚拟表**（`memory/fts_store.py`）：PG 没有 FTS5。好消息是它实际不依赖
  `MATCH`（注释写明"无论后端是否 FTS5，`search` 一律走 Python 侧过滤"），退化成
  普通表即可。
- **三个独立 SQLite 文件合并**（`ecom.db` / `traces.db` / `hitl.db`）：消费闸要跨库
  读 `hitl.db` 的 `count_pending()`，那是当前一个隐性耦合。
- **A–E 五组业务方法的逐组双后端验证**：目前只覆盖了队列、共享上下文、购物车、
  心跳、保留字往返这几条；C 组（`outreach_drafts` 的条件更新幂等，"消息只发一次"
  的唯一保证）**尚未在 PG 上验证**，这是风险最高的一组。

**默认后端仍是 `sqlite`**，行为逐字节不变；`db_backend=pg` 才走新路径。
配成 pg 却没给 dsn 时**刻意抛而不降级**——数据层是业务主链路，静默回落 SQLite 会让
两个实例各写各的库，那种数据分裂比启动失败难查得多（与队列载体的降级取舍**相反**，
因为队列在买家热路径上、丢一次投递远好过打死会话）。

---

C 组要额外小心：`review_outreach_draft` / `mark_outreach_sent` / `grant_coupon` 的
条件更新是"消息只发一次"的唯一保证，迁移后必须重跑
`tests/test_growth_api.py` 的幂等用例与 `test_collab_parallel.py` 的并发用例。

---

## Task PG4: 数据迁移与切换

- 一次性导出/导入脚本（`app/scripts/migrate_to_pg.py`），**幂等可重跑**
- 校验脚本：逐表比对行数 + 抽样比对内容，**不比对就不算迁完**
- 切换用配置（`db_backend: sqlite | postgres`），保留回退能力
- `traces.db` / `hitl.db` 一并合并进同库不同 schema

---

## 四、什么时候该启动这件事

**不是现在。** 三个触发条件，满足任一条再启动：

1. **要多实例部署**（这是唯一真正的驱动力——SQLite 单写者会先卡住下单/退款/审批）
2. 单库体量到百万行级，或并发写开始出现可观测的 busy_timeout 等待
3. 需要跨语言/跨服务访问同一份数据

在此之前，当前形态（SQLite + WAL + 条件更新幂等）对单店单实例是**更优解**：
零运维组件、ACID 更强、备份就是拷一个文件。

---

## 五、迁移完成后能说的一句话

> 数据层与协作队列同库同事务。队列用 `FOR UPDATE SKIP LOCKED` 实现多实例安全的
> 幂等认领，同时保留优先级调度、事件三态状态机、按 `correlation_id` 的全链路回溯
> ——这三样是 Kafka 与 Redis Stream 都不提供的。载体切换由 `EventBus` 接口隔离，
> 三个 Agent 的代码零改动。
