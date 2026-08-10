# 协作 worker 并行化 实施方案（P1/P2 + L2）

**Goal:** 让多智能体协作从「单线程串行」变成「有界并行」，把一轮协作的最坏耗时从
分钟级压到十几秒级；同时把并行会放大的两处并发缺陷先补掉，使并行**不引入**任何
新的正确性风险。

**范围:** 本方案只做 P1(SQLite 并发基座) + P2(草稿去重下沉数据层) + L2(Agent
内部起草并行)。**不做** L1(事件间并行)与 L4(四段并行)——理由见「刻意不做」。

---

## 一、现状与量级

协作 worker (`app/scripts/agent_collab.py`) 是裸 `while True: cycle(); sleep(interval)`，
`cycle()` 内四段串行：`scan → consume(analyst→growth) → attribute → followup`。

真正的耗时集中在**含 LLM 调用的两处**，`settings.collab_llm_timeout_s = 30`：

| 位置 | 串行结构 | 最坏耗时 |
|---|---|---|
| `bus.consume(target, handler, limit=20)` | 20 条事件逐条 `handler(ev)` | 20 × 30s = **600s** |
| `collab.handle_insight` 起草循环 | 一条诊断下 N 个商机逐个 `_llm_draft` | 20 × 30s = **600s** |

两处都是**天然可并行**的：事件之间 `correlation_id` 不同、商机之间买家不同，彼此
无数据依赖。

---

## 二、企业级对标：为什么是「有界并行 + 数据层判重」

参考成熟客服/工单系统（阿里小蜜一路的异步作业、Salesforce/ServiceNow 的
approval + queue 模型）在这类"异步批处理 + 外部模型调用"场景上的共识做法：

1. **并行度必须有界**，且与下游写入能力匹配。无界并发打外部模型端点会撞限流，
   打本地库会撞写锁——两边都表现为"越并行越慢"。
2. **判重必须落在存储层**，不能靠应用层的内存快照。应用层去重只在单线程下成立；
   一旦并行或多实例，"先查后插"必然出现重复。本项目自己在
   `Database.start_followup` 的注释里已经把这条写死过：
   > 并发下唯一可靠的判重方式是让约束顶上去，而不是先查后插
3. **认领与结束都用条件更新**。本项目 `claim_events` / `finish_event` /
   `review_outreach_draft` 已经是这个形态，**并行不需要改它们**——这也是本方案
   成本低的根本原因：幂等地基早就打好了。
4. **读写分离的并发基座**。SQLite 对应物是 WAL：读者不阻塞写者、写者不阻塞读者，
   写者之间仍串行但走排队而不是立刻报错。

---

## 三、Global Constraints

1. **不动安全边界**：consent 门、工作流守卫、HITL 升级、人工审批闸、营销"只产
   草稿"这条线一律不改。并行只改「多快」，绝不改「能做什么」。
2. **并行必须可一键关掉**，关掉后行为与本方案之前**逐字节一致**。
3. **不引入新的失败模式**：并行路径里任何单条失败，隔离在那一条上，不拖垮整批
   （与现有 `consume()` 的单条 failed 语义一致）。
4. **去重语义不得放宽**。并行后同一个 (买家, 订单, 商机类型) 仍然最多一条待审
   草稿——这是店主注意力的保护线，不是性能优化的可牺牲项。
5. 中文注释与文案；补齐回归测试；全量套件必须保持全绿。

---

## Task P1: SQLite 并发基座（WAL + busy_timeout）

**Files:** `app/db/database.py`、`tests/test_db_concurrency.py`(新增)

**问题:** `Database.connect()` 现在是裸 `sqlite3.connect(path)`——默认 rollback
journal、默认 5 秒锁等待。rollback journal 下**写者与读者互斥**，协作 worker 每
处理一条事件要写好几次（`share` / `publish` / `create_outreach_draft` /
`finish_event`），并行度一上来就会撞出 `database is locked`。

**做法:**

- `connect()` 里开 `PRAGMA journal_mode=WAL` 与 `PRAGMA busy_timeout=<ms>`。
- 超时值走配置 `db_busy_timeout_ms`（默认 5000），不写死。
- WAL 是**数据库文件级**的持久属性，设一次即可；但 `journal_mode` 的 PRAGMA 每次
  连接执行代价极低（已是 WAL 时是空操作），放在 `connect()` 里最省心，也避免
  "某条路径绕过了初始化"。
- **只读内存库要跳过**：`:memory:` 不支持 WAL，执行会失败——测试里有内存库用法
  时必须容错（fail-soft，退回默认 journal）。

**验收:**
- 新增测试：两个线程各写 200 行到不同表，全部成功、无 `database is locked`。
- 新增测试：`journal_mode` 查询返回 `wal`。
- 既有全量套件不变绿。

---

## Task P2: 草稿去重下沉到数据层

**Files:** `app/db/database.py`、`app/multi_agent/collab.py`、`tests/test_growth_tools.py`
或新增 `tests/test_outreach_dedupe.py`

**问题:** 当前去重完全在 `collab.handle_insight` 的应用层：

```python
seen = get_db().pending_outreach_targets()   # 读一次快照
for opp in opportunities:
    if key in seen: continue
    ...起草...
    seen.add(key)          # 只在本进程内存里加
```

串行时正确；**并行后两条诊断各自读到同一份快照**，同一个买家会被排两条几乎相同
的草稿——店主挨个批完就等于给同一个人连发两条。这正是本项目在别处已经明令禁止
的"先查后插"。

**做法:**

- 加部分唯一索引（照抄 `idx_followups_active_unique` 的既有手法）：

  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_draft_unique
      ON outreach_drafts(user_id, order_id, opportunity_type)
      WHERE status = 'draft';
  ```

  只约束 `status='draft'`（等人看的那些）。已 approved/sent/rejected 的不参与——
  与 `pending_outreach_targets` 的既有口径**完全一致**，不新造第二套语义：那些是
  "已经处理过的历史"，不该永久封杀对同一个订单的再次触达。

- `create_outreach_draft` 捕获 `sqlite3.IntegrityError` → 返回 `None`（不抛）。
  与 `start_followup` 的返回约定一致：`None` = "已有一条，本次不重复排"，不是错误。

- 调用方按 `None` 处理：
  - `collab.handle_insight`：计入 `skipped_duplicate`，不计入 `drafted`。
  - `followup.run_due`：跟进链那一步同样可能撞重，按"本步跳过起草但仍推进步数"处理。
  - `growth.draft_outreach`（Agent 工具）：返回 `{"success": False, "error": "该买家
    的这条商机已有待审草稿，无需重复起草"}`——**给模型一句能读懂的话**，让它别重试。

- **应用层的 `seen` 保留**：它从"正确性的唯一防线"降级为"省一次无谓 LLM 调用"的
  优化。删掉它会让每条重复商机都白花一次 `_llm_draft` 才被数据库拒绝。

**⚠ 迁移风险(必须处理):** 存量库里可能**已经存在**违反新约束的重复 draft 行
（当前没有任何约束拦过它们）。直接 `CREATE UNIQUE INDEX` 会在 `init_schema()` 时
抛 `IntegrityError`，导致**服务起不来**。

处理方式：建索引前先做一次幂等清理——同一 (user_id, order_id, opportunity_type)
的 draft 行只保留 `id` 最大的一条（最新的那条内容最贴近当前情境），其余置为
`rejected` 并写明原因，**不物理删除**（审计可追溯）。清理与建索引都放在
`init_schema()` 里，`IF NOT EXISTS` 保证重复执行无副作用。

**验收:**
- 并发两条线程对同一 (user, order, kind) 起草，只有一条成功落库，另一条拿到 `None`。
- 存量重复数据场景：预置 3 条重复 draft → `init_schema()` 后剩 1 条 draft + 2 条
  rejected，索引建成功。
- 不同 kind / 不同订单 / 已 sent 的历史行不受约束影响。

---

## Task L2: 起草并行（Agent 内部）

**Files:** `app/multi_agent/collab.py`、`app/config/settings.py`、`tests/test_collab_parallel.py`(新增)

**做法:** `handle_insight` 的起草循环改成有界线程池。**关键在于职责切分**：

```
主线程(串行)：遍历商机 → 算去重键 → 命中 seen 则跳过 → 未命中则占坑(seen.add)
                                                    ↓ 提交
线程池(并行)：_llm_draft(...)  →  create_outreach_draft(...)  →  _attach_correlation(...)
```

**为什么占坑留在主线程**：`seen` 是普通 set，多线程增删需要锁；而占坑本身是纯内存
操作、耗时可忽略。把它留在主线程，`seen` 的语义与串行版**完全相同**，不需要任何
同步原语，也不会出现"两个线程同时判空都通过"。并行的只有真正慢的那一段（一次
LLM 调用 + 几次写库）。

**并发度**：`settings.collab_max_parallel`，默认 4。不宜大：每个线程都要写 SQLite，
WAL 下写者仍串行排队，开到 16 只会让线程互相等锁，并且同时增大打模型端点限流的
概率。

**开关**：`settings.collab_parallel_enabled`，默认 True；关掉走原来的串行 for 循环，
逐字节回退。

**异常语义不变**：单个商机起草失败（模型抖动/超时）仍然只跳过那一个，记
`logger.exception` 带全 traceback + 商机身份，不拖垮整批。线程池里抛出的异常在
`future.result()` 处捕获，语义与现在的 `try/except continue` 一致。

**顺序性说明**：并行后草稿的**落库顺序**不再严格等于商机的优先级顺序。这不影响
正确性——控制台按 `priority_score` 展示，审批也不依赖 id 顺序。但要在代码里写明，
免得后来者以为 id 递增等于优先级递减。

**验收:**
- 并行开关关掉 → 与串行版产出完全一致（同样的草稿集合）。
- 8 个商机、每次 `_llm_draft` 打桩 sleep 0.2s：并行版挂钟耗时显著低于串行版。
- 单个商机起草抛异常 → 其余 7 条照常落库，`drafted == 7`。
- 并行路径下去重仍然生效（同一买家两条相同商机只落一条）。

---

## 刻意不做（写进方案，避免后来者以为漏了）

**L1 事件间并行（`bus.consume` 内并发跑 handler）**——收益同样大，但改动面比 L2
广（要动总线这个所有协作都经过的公共组件），本轮先不动。建议在 L2 上线并观察一
轮之后再做，届时 P1/P2 已经把基座铺好，L1 基本只是把 `for ev in events` 换成线程池。

**L4 四段并行（scan/consume/attribute/followup）**——**明确不做**。
`run_once()` 的 docstring 写明了「先 consume(ANALYST) 再 consume(GROWTH)」是刻意的：
参谋段发布的 `insight.diagnosis` 在同一次调用里就能被营销段消费，一条新信号调一次
`run_once()` 就走完全链路。四段并行会打破这个保证，代价是每条链多等一个
`--interval`（默认 60s），换来的收益几乎为零——这四段本身都不慢。

**多 worker 进程**——不需要单独做。P1 补完 WAL、P2 把判重下沉之后，多进程并行
**自动就是安全的**（`claim_events` 的条件更新本来就为此设计）。运维层面起两个
`--loop` 进程即可，不需要代码改动。

---

## 一个必须同时交代的局限

**并行不解决「worker 死了没人知道」。** 反而让它更隐蔽：串行时看 stdout 能大致判断
进度，并行后输出交错。当前 `consume()` 的 failed 事件留在表里但**没有告警、没有面板、
没有列出入口**（时间线必须先知道 `correlation_id`）。

这不在本方案范围内，但**必须在交付说明里写明**，并建议作为紧接着的下一项：
一个 `GET /api/admin/collab/failed` + 控制台红色计数 + worker 的 last-success 时间戳。
