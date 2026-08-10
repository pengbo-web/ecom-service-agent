# 总线规则引擎补全:事件 Schema + 优先级 + 状态拦截

**Goal:** 把「事件驱动 + 隐式编排」这套架构里**防失控的三条规则**补齐——事件标准、
优先级、状态拦截。路由表(上一轮)解决了"谁来处理",这一轮解决"按什么顺序处理、
什么情况下不处理、以及事件本身长得对不对"。

**范围:** 只动总线层。不动 Agent 能力边界、不动人工闸、不动幂等语义。

---

## 一、为什么这三条是一组

事件驱动架构的公认代价是:**松耦合的另一面是失控**。发布方不知道谁在消费,
消费方不知道谁在发布,于是三类问题会同时出现:

| 问题 | 后果 | 对策 |
|---|---|---|
| 事件长得不对 | 消费方 `.get()` 摸空,**静默不工作** | 事件 Schema |
| 急事排在闲事后面 | 延迟敏感的事件被例行任务堵住 | 优先级 |
| 该停的时候没停 | 服务侧正在救火,营销还在推销 | 状态拦截 |

第一条在本项目**因为上一轮的路由化而变严重了**:`kind` 字段以前只被处理器读,
现在还决定路由(`_marketing_worthy`)。生产方漏发一个字段 → 谓词判否 →
`resolve()` 返回 `[]` → `publish` 返回 None → **整条营销链静默消失**,没有异常、
没有 failed 事件、时间线上什么都没有。失败形态是"什么都没发生",这是最难查的一种。

---

## 二、Global Constraints

1. **不动安全边界**:consent 门、工具子集、人工审批闸一律不改。
2. **`publish` 仍在买家会话热路径上** —— 因此发布侧的校验**只能是纯内存的**,
   且校验不通过时**照常发布**(记 warning),绝不能因为一条埋点的 schema 问题
   让买家那一轮失败。
3. **状态拦截放在消费侧,不是发布侧**。两个理由:发布侧读库会把 IO 加进买家
   热路径;更重要的是**状态在发布与消费之间会变**,发布时判等于用过期状态做决定。
4. 幂等语义不变:`claim_events` / `finish_event` / `reclaim_stale_events` 的条件
   更新纪律不动。
5. 中文注释;补齐回归;全量保持全绿。

---

## Task S1: 事件 Schema(声明 + 校验 + 可见)

**Files:** `app/multi_agent/event_schema.py`(新增)、`app/multi_agent/bus.py`、测试

- 每个事件类型声明**必需字段**(纯常量表,不引入校验框架——payload 是自由 dict,
  用 Pydantic 会逼所有发布点改造,收益不抵成本)
- `publish` 校验:缺字段 → `logger.warning` 带事件类型与缺失字段名,**照常发布**
- **关键的一条测试**:路由谓词读到的每个字段,都必须在该事件类型的必需字段里声明。
  这条防的是"谓词悄悄依赖了一个没人保证会有的字段"——它是本轮识别的那个静默
  失败的根源。

**为什么不做成 fail-closed**:发布点之一在买家热路径上(`streaming.py` 的
转人工埋点)。一条埋点的 schema 问题不该让买家那一轮出错;而漏发的后果由
warning + 下面的测试兜住。

---

## Task S2: 优先级

**Files:** `app/db/database.py`、`app/multi_agent/routing.py`、`app/multi_agent/bus.py`、测试

- `agent_events` 加 `priority INTEGER NOT NULL DEFAULT 0`(带旧库 ALTER 迁移)
- `claim_events` 改 `ORDER BY priority DESC, id ASC` —— 同优先级仍是 FIFO
- 优先级在**路由表里声明**(`Subscription.priority`),支持常量或
  `(payload) -> int` 谓词——后者是必要的:本项目所有异常都走同一个
  `signal.anomaly` 事件类型,"买家刚被转人工"与"例行退款率扫描"的区别在
  payload 的 `kind` 里,不在事件类型上

**诚实交代**:当前总线上**没有买家或店主正在等的事件**(买家那一轮的回复根本不
经过总线),所以优先级今天的实际收益有限。补它是为了两件事:一是
`service_escalation`(买家刚被转人工)确实比例行扫描更该先看;二是将来任何一个
延迟敏感的事件类型接进来时,机制已经在了,不必再改表。

---

## Task S3: 状态拦截(消费闸)

**Files:** `app/multi_agent/routing.py`、`app/multi_agent/bus.py`、`app/db/database.py`、测试

在 `consume()` 里、调 handler **之前**加一道闸:

```
claim → 【状态闸】→ handler → finish(done/failed)
              ↓ 拒绝
         finish(skipped)
```

- 闸按 target agent 配置,签名 `(event) -> (allow: bool, reason: str)`
- 被拦的事件落 **`status='skipped'`**,不是 done 也不是 failed:
  - 不是 `done` —— 它并没有被处理,记成 done 就是账本撒谎
  - 不是 `failed` —— 它没有出错,混进失败列表会让真正的故障被淹没
  - `skipped` 不会被 `claim_events`(只挑 pending)再捞到,也不会进
    `list_failed_events`,但在时间线上如实可见
- **闸失败一律放行(fail-open)**:这道闸是"少做一点事"的优化,不是安全边界。
  真正的安全边界是人工审批闸,它在后面且从不失效。闸自己出错就把事件放过去,
  让既有的下游约束接着守——反过来 fail-closed 会让一次读库抖动静默吞掉协作链。

**首条规则:营销静默期。** 未结人工工单数 ≥ 阈值时,拦截 `growth` 的事件消费。

理由:服务侧正在救火(一堆买家等着人工处理)时,同时启动推销是明显的时机错误。
这条规则用的是已有数据(`HandoffQueue.count_pending()`),不新增采集。
阈值 `collab_marketing_pause_open_handoffs` 可配,`0` = 关掉这条闸。

**为什么不用 `arbitration.check_outreach_allowed`**:那条是**按买家**判的
(该买家是否正在被人工处理),而 `insight.diagnosis` 的 payload 里
`subject` 是**商品**,没有 user_id——按买家的仲裁在这一跳上无从下手。
它已经在投递侧(`approve_draft`)和跟进侧生效,是正确的位置;这里需要的是一条
**店铺级**的状态规则,两者互补而不是重复。

---

## 刻意不做

**会话内实时异常检测**(引文里的 `detector.is_anomaly(product_id, "size_question")`
——"该商品尺码咨询量突增 +120%")。本项目的异常发现是 `anomaly_scan` 定时批量
扫描,不是会话内实时判定。补它需要新增"按商品的咨询量时序"这个采集维度,是
**新功能而不是架构调整**,不在本方案范围。两者产出的事件形状相同,补上之后
直接复用现有总线,不需要再动这一层。

**Saga 协调器**。本项目没有跨 Agent 的多步事务场景:退款是单 Agent 内的单步
操作 + consent 门 + 人工闸;唯一的多步链路(审批→发券→投递→标记→起跟进链)
已有手写补偿(`_revert_after_failure`,且区分可重试与永久拒绝)。硬套一个通用
协调器是为不存在的问题造框架。
